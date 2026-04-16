#include "postgres.h"

#ifndef FRONTEND
#include "storage/ipc.h"
#include "storage/lwlock.h"
#include "storage/shmem.h"
#include "miscadmin.h"

/*
 * PostgreSQL sets redirection_done once backend stderr is redirected to the
 * syslogger pipe. In that configuration elog() already uses chunked,
 * pipe-atomic writes, so extra serialization is unnecessary.
 */
extern bool redirection_done;
#else
#include <pthread.h>
#endif

#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h> // For gettimeofday in log_message

#include "time_instr.h"

#include "lib/stringinfo.h"
#include "port.h" // for printf from PG

const char *timing_spot_names[_NUM_TIMING_SPOTS] = {TIMING_SPOTS(AS_STRING)};
const char *custom_stat_names[_NUM_CUSTOM_STATS] = {CUSTOM_STATS(STATS_AS_STRING)};

// Internal structure to hold data for a single timer
typedef struct
{
    const char *name;
    uint64_t total_ns;
    uint64_t count;
    struct timespec start_time;
	bool active;
	bool paused;
    uint64_t custom_stats[_NUM_CUSTOM_STATS]; // Array of custom statistics indexed by stat_key
} timer_stat_t;

// Global state for the logger
// static struct
// {
//     int num_timers;
//     timer_stat_t *stats;
// } logger_state = {0, NULL};
/* Original logger_state without identity persistence. */
// static struct
// {
//     int num_timers;
//     timer_stat_t *stats;
//     bool in_distributed_xact;
//     char *identity;
// } logger_state = {0, NULL, false, NULL};

static struct
{
    int num_timers;
    timer_stat_t *stats;
    bool in_distributed_xact;
    char *identity;
    bool identity_persist;
	char command_tag[LATENCY_REMOTE_COMMAND_TAG_MAXLEN];
	bool has_command_tag;
    /*
     * QUERY_ACTIVE_WALL spans one logical query cycle on the backend. It can
     * be paused by nested waits, so keep explicit state instead of inferring
     * it from the low-level timer fields.
     */
    bool query_active_wall_running;
    int query_active_wait_depth;
    bool query_wait_wall_running;
} logger_state = {0, NULL, false, NULL, false, "", false, false, 0, false};

/**
 * @brief Returns true if a timing spot should persist across a distributed transaction.
 *
 * These timers span multiple commands and must not be reset while a distributed
 * transaction is active.
 */
static bool
IsTransactionalTimingSpot(int timer_id)
{
    switch (timer_id)
    {
        case XACT_PROCESSING:
        case XACT_TS_SendRemoteCommand:
        case XACT_TS_GetRemoteCommandResult:
        case XACT_TS_WaitForConnections:
        case XACT_TS_coordinated_commit_abort:
        case XACT_TS_EndCommand:
        case XACT_TS_CoordinatorPrepare:
        case XACT_WAIT:
            return true;
        default:
            return false;
    }
}

#ifndef FRONTEND
typedef struct LoggerSharedState
{
    LWLock lock;
} LoggerSharedState;

static LoggerSharedState *logger_shared = NULL;

size_t LoggerShmemSize(void) { return sizeof(LoggerSharedState); }

void LoggerShmemInit(void)
{
    bool found;

    logger_shared = (LoggerSharedState *)ShmemInitStruct("Logger Shared State", LoggerShmemSize(), &found);

    if (!found)
    {
        LWLockInitialize(&logger_shared->lock, LWTRANCHE_LOGGER);
    }
}
#else
static pthread_mutex_t logger_print_mutex = PTHREAD_MUTEX_INITIALIZER;
#endif

int logger_init(int num_timers, const char *names[])
{
    if (logger_state.stats != NULL)
    {
        // Already initialized
        return 0;
    }

    logger_state.num_timers = num_timers;
    logger_state.stats = (timer_stat_t *)calloc(num_timers, sizeof(timer_stat_t));

    if (logger_state.stats == NULL)
    {
        perror("Failed to allocate memory for logger stats");
        return -1;
    }

    for (int i = 0; i < num_timers; ++i)
    {
        int j;
        logger_state.stats[i].name = names[i] ? names[i] : "Unnamed Timer";
        /* Initialize all custom stats to 0 */
        for (j = 0; j < _NUM_CUSTOM_STATS; ++j)
        {
            logger_state.stats[i].custom_stats[j] = 0;
        }
    }

    return 0;
}

/**
 * @brief Updates whether the logger is inside a distributed transaction.
 *
 * This flag controls whether logger_print_timings/logger_reset preserve
 * transactional timers across commands.
 */
void
logger_set_distributed_xact_state(bool in_distributed_xact)
{
    logger_state.in_distributed_xact = in_distributed_xact;
}

/**
 * @brief Set or clear an identity string for timing reports.
 *
 * The identity is copied so callers can pass stack-allocated buffers.
 */
void
logger_set_identity(const char *identity)
{
    if (logger_state.identity != NULL)
    {
        free(logger_state.identity);
        logger_state.identity = NULL;
    }

    if (identity == NULL || identity[0] == '\0')
    {
        return;
    }

    logger_state.identity = strdup(identity);
}

/**
 * @brief Controls whether the current identity should persist across commands.
 *
 * When persist is true, logger_reset keeps the identity even when not in a
 * distributed transaction so explicit BEGIN/COMMIT blocks stay tagged.
 */
void
logger_set_identity_persist(bool persist)
{
    /* Keep identity until logger_reset clears it when persistence is disabled. */
    logger_state.identity_persist = persist;
}

bool
logger_identity_persist_enabled(void)
{
	return logger_state.identity_persist;
}

bool
logger_distributed_xact_active(void)
{
	return logger_state.in_distributed_xact;
}

const char *
logger_get_identity(void)
{
	return logger_state.identity;
}

void
logger_set_command_tag(const char *commandTag)
{
	logger_state.command_tag[0] = '\0';
	logger_state.has_command_tag = false;

	if (commandTag == NULL || commandTag[0] == '\0')
	{
		return;
	}

	strlcpy(logger_state.command_tag, commandTag, sizeof(logger_state.command_tag));
	logger_state.has_command_tag = true;
}

const char *
logger_get_command_tag(void)
{
	return logger_state.has_command_tag ? logger_state.command_tag : NULL;
}

/*
 * Reset the active-wall/query-wait bookkeeping without touching the timer
 * arrays themselves. Callers use this whenever a logical query cycle is
 * finalized or when logger_reset() discards non-transactional state.
 */
static void
ResetQueryActiveWallState(void)
{
    logger_state.query_active_wall_running = false;
    logger_state.query_active_wait_depth = 0;
    logger_state.query_wait_wall_running = false;
}

/**
 * @brief Adds a value to a custom statistic for a specific timer.
 *
 * This function allows tracking additional metrics beyond just timing,
 * such as bytes processed, bytes sent, etc. The stat is identified by
 * an enum key (defined in custom_stats.h), and the value is accumulated.
 *
 * @param timer_id The timer ID to add the stat to.
 * @param stat_key The custom_stat_key_t enum value for the stat.
 * @param value The value to add to the stat.
 */
void timing_add_stat(int timer_id, int stat_key, uint64_t value)
{
    if (timer_id < 0 || timer_id >= logger_state.num_timers || logger_state.stats == NULL)
        return;

    if (stat_key < 0 || stat_key >= _NUM_CUSTOM_STATS)
        return;

    logger_state.stats[timer_id].custom_stats[stat_key] += value;
}
void timing_start(int timer_id)
{
    if (timer_id < 0 || timer_id >= logger_state.num_timers)
        return;
    clock_gettime(CLOCK_MONOTONIC_RAW, &logger_state.stats[timer_id].start_time);
	logger_state.stats[timer_id].active = true;
	logger_state.stats[timer_id].paused = false;
}

static void
timing_accumulate(int timer_id, bool increment_count)
{
    if (timer_id < 0 || timer_id >= logger_state.num_timers)
        return;

	if (!logger_state.stats[timer_id].active)
		return;

    struct timespec end_time;
    clock_gettime(CLOCK_MONOTONIC_RAW, &end_time);

    uint64_t start_ns = (uint64_t)logger_state.stats[timer_id].start_time.tv_sec * 1000000000 +
                        logger_state.stats[timer_id].start_time.tv_nsec;
    uint64_t end_ns = (uint64_t)end_time.tv_sec * 1000000000 + end_time.tv_nsec;

    uint64_t elapsed_ns = end_ns - start_ns;

    logger_state.stats[timer_id].total_ns += elapsed_ns;
	if (increment_count)
		logger_state.stats[timer_id].count++;
	logger_state.stats[timer_id].active = false;
	logger_state.stats[timer_id].paused = !increment_count;
}

void timing_end(int timer_id)
{
	timing_accumulate(timer_id, true);
}

void timing_pause(int timer_id)
{
	timing_accumulate(timer_id, false);
}

void timing_resume(int timer_id)
{
	if (timer_id < 0 || timer_id >= logger_state.num_timers)
		return;

	if (!logger_state.stats[timer_id].paused)
		return;

	timing_start(timer_id);
}

void
logger_query_active_wall_start(void)
{
    if (logger_state.stats == NULL)
        return;

    if (logger_state.query_active_wall_running)
        return;

    ResetQueryActiveWallState();
    timing_start(QUERY_ACTIVE_WALL);
    logger_state.query_active_wall_running = true;
}

void
logger_query_active_wall_stop(void)
{
    if (!logger_state.query_active_wall_running)
        return;

    /*
     * End any in-flight excluded wait first. Resume QUERY_ACTIVE_WALL before
     * ending it so timing_end() can increment the logical query-cycle count.
     */
    if (logger_state.query_wait_wall_running)
    {
        timing_end(QUERY_WAIT_WALL);
        logger_state.query_wait_wall_running = false;
    }

    if (logger_state.query_active_wait_depth > 0)
    {
        logger_state.query_active_wait_depth = 0;
        timing_resume(QUERY_ACTIVE_WALL);
    }

    timing_end(QUERY_ACTIVE_WALL);
    ResetQueryActiveWallState();
}

void
logger_query_active_wall_pause_wait(void)
{
    if (!logger_state.query_active_wall_running)
        return;

    if (logger_state.query_active_wait_depth == 0)
    {
        timing_pause(QUERY_ACTIVE_WALL);
        timing_start(QUERY_WAIT_WALL);
        logger_state.query_wait_wall_running = true;
    }

    logger_state.query_active_wait_depth++;
}

void
logger_query_active_wall_resume_wait(void)
{
    if (!logger_state.query_active_wall_running)
        return;

    if (logger_state.query_active_wait_depth <= 0)
        return;

    logger_state.query_active_wait_depth--;
    if (logger_state.query_active_wait_depth > 0)
        return;

    if (logger_state.query_wait_wall_running)
    {
        timing_end(QUERY_WAIT_WALL);
        logger_state.query_wait_wall_running = false;
    }

    timing_resume(QUERY_ACTIVE_WALL);
}

bool
logger_query_active_wall_is_running(void)
{
    return logger_state.query_active_wall_running;
}

/*
 * Legacy logger_print_timings implementation retained for reference. It
 * relied on stdout buffering, which could interleave output across processes.
 * The new implementation below emits via elog() to leverage PostgreSQL's
 * unbuffered logging path.
 */
#if 0
void logger_print_timings(void)
{
    /* Acquire mutex to ensure logger_print_timings is not called concurrently. */
#ifndef FRONTEND
    if (logger_shared == NULL)
    {
        return;
    }

    LWLockAcquire(&logger_shared->lock, LW_EXCLUSIVE);

    if (logger_state.stats == NULL)
    {
        printf("Logger not initialized\n");
        LWLockRelease(&logger_shared->lock);
        return;
    }
#else
    if (pthread_mutex_lock(&logger_print_mutex) != 0)
    {
        printf("Failed to acquire logger_print_timings mutex\n");
        return;
    }

    if (logger_state.stats == NULL)
    {
        printf("Logger not initialized\n");
        pthread_mutex_unlock(&logger_print_mutex);
        return;
    }
#endif

    /* If nothing has been recorded (no timer has a non-zero count),
       do not print anything at all. */
    int any_recorded = 0;
    for (int i = 0; i < logger_state.num_timers; ++i)
    {
        if (logger_state.stats[i].count > 0)
        {
            any_recorded = 1;
            break;
        }
    }
    if (!any_recorded)
    {
#ifndef FRONTEND
        LWLockRelease(&logger_shared->lock);
#else
        pthread_mutex_unlock(&logger_print_mutex);
#endif
        return;
    }

    // Print header for nanosecond timing report
    printf("\n--- Timing Report (Nanoseconds) ---\n");
    printf("%-30s | %10s | %18s | %18s | %s\n", "Timer Name", "Count", "Total Time (ns)", "Average Time (ns)",
           "Custom Stats");
    printf("-----------------------------------------------------------------------------------------------------------"
           "---\n");

    for (int i = 0; i < logger_state.num_timers; ++i)
    {
        timer_stat_t *stat = &logger_state.stats[i];
        if (stat->count == 0)
            continue;

        // Use nanoseconds for both total and average
        uint64_t total_ns = stat->total_ns;
        uint64_t avg_ns = stat->count ? (stat->total_ns / stat->count) : 0;

        printf("%-30s | %10llu | %18llu | %18llu | ", stat->name, (unsigned long long)stat->count,
               (unsigned long long)total_ns, (unsigned long long)avg_ns);

        /* Print custom stats in comma-separated key=value format */
        int first = 1;
        int j;
        for (j = 0; j < _NUM_CUSTOM_STATS; ++j)
        {
            /* Only print non-zero stats */
            if (stat->custom_stats[j] > 0)
            {
                if (!first)
                {
                    printf(", ");
                }
                printf("%s=%llu", custom_stat_names[j], (unsigned long long)stat->custom_stats[j]);
                first = 0;
            }
        }
        printf("\n");
    }
    printf("-----------------------------------------------------------------------------------------------------------"
           "---\n");

    free(logger_state.stats);
    logger_state.stats = NULL;
    logger_state.num_timers = 0;

    // do logger init again just in case
    logger_init(_NUM_TIMING_SPOTS, timing_spot_names);

#ifndef FRONTEND
    LWLockRelease(&logger_shared->lock);
#else
    pthread_mutex_unlock(&logger_print_mutex);
#endif
}
#endif /* Legacy logger_print_timings() */

/**
 * @brief Emit timing statistics through elog() so each report is unbuffered.
 *
 * When inside a distributed transaction, only non-transactional timers are
 * printed and reset so XACT_* timers can span multiple commands.
 */
void logger_print_timings(void)
{
#ifndef FRONTEND
    bool need_backend_log_lock = (!redirection_done && MyBackendType != B_LOGGER);

    if (logger_state.stats == NULL)
    {
        /*
         * Original code serialized this elog() with logger_shared->lock.
         * Keep that fallback only when stderr is not redirected to the
         * syslogger pipe, because the syslogger path already preserves one
         * whole message without a second lock.
         */
        /* LWLockAcquire(&logger_shared->lock, LW_EXCLUSIVE); */
        if (need_backend_log_lock && logger_shared != NULL)
            LWLockAcquire(&logger_shared->lock, LW_EXCLUSIVE);
        elog(LOG, "Logger not initialized");
        if (need_backend_log_lock && logger_shared != NULL)
            LWLockRelease(&logger_shared->lock);
        /* LWLockRelease(&logger_shared->lock); */
        return;
    }
#else
    if (pthread_mutex_lock(&logger_print_mutex) != 0)
    {
        fprintf(stderr, "Failed to acquire logger_print_timings mutex\n");
        fflush(stderr);
        return;
    }

    if (logger_state.stats == NULL)
    {
        fprintf(stderr, "Logger not initialized\n");
        fflush(stderr);
        pthread_mutex_unlock(&logger_print_mutex);
        return;
    }
#endif

    /* If nothing has been recorded (no timer has a non-zero count),
       do not print anything at all. */
    int any_recorded = 0;
    for (int i = 0; i < logger_state.num_timers; ++i)
    {
        bool include_timer = true;
        if (logger_state.in_distributed_xact)
        {
            include_timer = !IsTransactionalTimingSpot(i);
        }

        if (include_timer && logger_state.stats[i].count > 0)
        {
            any_recorded = 1;
            break;
        }
    }
    if (!any_recorded)
    {
#ifdef FRONTEND
        pthread_mutex_unlock(&logger_print_mutex);
#endif
        return;
    }

    /* Compose the full report before logging so a single elog() call emits it atomically. */
    StringInfoData buf;
    initStringInfo(&buf);

    if (logger_state.identity != NULL)
    {
        appendStringInfo(&buf, "DistributedTransactionId: %s\n", logger_state.identity);
    }
    else
    {
        appendStringInfoString(&buf, "DistributedTransactionId: \n");
    }
	appendStringInfo(&buf, "RemoteCommandTag: %s\n",
					 logger_state.has_command_tag ? logger_state.command_tag : "");
    appendStringInfoString(&buf, "\n--- Timing Report (Nanoseconds) ---\n");
    appendStringInfo(&buf, "%-30s | %10s | %18s | %18s | %s\n", "Timer Name", "Count", "Total Time (ns)",
                     "Average Time (ns)", "Custom Stats");
    appendStringInfoString(
        &buf,
        "----------------------------------------------------------------------------------------------------------"
        "-\n");

    for (int i = 0; i < logger_state.num_timers; ++i)
    {
        timer_stat_t *stat = &logger_state.stats[i];
        bool include_timer = true;
        if (logger_state.in_distributed_xact)
        {
            include_timer = !IsTransactionalTimingSpot(i);
        }

        if (!include_timer || stat->count == 0)
            continue;

        // Use nanoseconds for both total and average
        uint64_t total_ns = stat->total_ns;
        uint64_t avg_ns = stat->count ? (stat->total_ns / stat->count) : 0;

        appendStringInfo(&buf, "%-30s | %10llu | %18llu | %18llu | ", stat->name, (unsigned long long)stat->count,
                         (unsigned long long)total_ns, (unsigned long long)avg_ns);

        /* Print custom stats in comma-separated key=value format */
        int first = 1;
        int j;
        for (j = 0; j < _NUM_CUSTOM_STATS; ++j)
        {
            /* Only print non-zero stats */
            if (stat->custom_stats[j] > 0)
            {
                if (!first)
                {
                    appendStringInfoString(&buf, ", ");
                }
                appendStringInfo(&buf, "%s=%llu", custom_stat_names[j], (unsigned long long)stat->custom_stats[j]);
                first = 0;
            }
        }
        appendStringInfoChar(&buf, '\n');
    }
    appendStringInfoString(
        &buf,
        "----------------------------------------------------------------------------------------------------------"
        "-\n");

#ifndef FRONTEND
    /*
     * Original code held logger_shared->lock around this elog() to enforce a
     * deterministic order between backends. That lock now dominates the
     * coordinator's post-CommandComplete tail. Keep it only for the unusual
     * direct-console case; the normal syslogger path already emits each
     * message intact via the chunked pipe protocol.
     */
    /* LWLockAcquire(&logger_shared->lock, LW_EXCLUSIVE); */
    if (need_backend_log_lock && logger_shared != NULL)
        LWLockAcquire(&logger_shared->lock, LW_EXCLUSIVE);
    elog(LOG_SERVER_ONLY, "%s", buf.data);
    if (need_backend_log_lock && logger_shared != NULL)
        LWLockRelease(&logger_shared->lock);
    /* LWLockRelease(&logger_shared->lock); */
#else
    fprintf(stderr, "%s\n", buf.data);
    fflush(stderr);
#endif

    pfree(buf.data);

    /* Reset after printing, respecting transactional timers if needed. */
    logger_reset();

#ifdef FRONTEND
    pthread_mutex_unlock(&logger_print_mutex);
#endif
}

// free and re-init the logger
#if 0
void logger_reset()
{
    if (logger_state.stats != NULL)
    {
        free(logger_state.stats);
        logger_state.stats = NULL;
        logger_state.num_timers = 0;
    }
    logger_init(_NUM_TIMING_SPOTS, timing_spot_names);
}
#endif /* disabled logger_reset */

/*
 * Original logger_reset implementation without identity persistence.
 */
#if 0
/**
 * @brief Reset timing stats based on distributed transaction state.
 *
 * When a distributed transaction is active, keep transactional timers intact
 * and clear only non-transactional timers so they don't accumulate across commands.
 */
void logger_reset()
{
    if (logger_state.stats == NULL)
    {
        return;
    }

    if (logger_state.in_distributed_xact)
    {
        for (int i = 0; i < logger_state.num_timers; ++i)
        {
            if (IsTransactionalTimingSpot(i))
                continue;

            timer_stat_t *stat = &logger_state.stats[i];
            stat->total_ns = 0;
            stat->count = 0;
            memset(&stat->start_time, 0, sizeof(stat->start_time));
			stat->active = false;
			stat->paused = false;
            memset(stat->custom_stats, 0, sizeof(stat->custom_stats));
        }

        ResetQueryActiveWallState();
        return;
    }

    free(logger_state.stats);
    logger_state.stats = NULL;
    logger_state.num_timers = 0;
    logger_state.in_distributed_xact = false;
    if (logger_state.identity != NULL)
    {
        free(logger_state.identity);
        logger_state.identity = NULL;
    }

    logger_init(_NUM_TIMING_SPOTS, timing_spot_names);
}
#endif /* disabled logger_reset without identity persistence */

/**
 * @brief Reset timing stats based on distributed transaction and identity state.
 *
 * When a distributed transaction is active, keep transactional timers intact
 * and clear only non-transactional timers so they don't accumulate across commands.
 * When not in a distributed transaction, reset all timers but preserve the
 * identity if identity_persist is true.
 */
void logger_reset()
{
    if (logger_state.stats == NULL)
    {
        return;
    }

    if (logger_state.in_distributed_xact)
    {
        for (int i = 0; i < logger_state.num_timers; ++i)
        {
            if (IsTransactionalTimingSpot(i))
                continue;

            timer_stat_t *stat = &logger_state.stats[i];
            stat->total_ns = 0;
            stat->count = 0;
            memset(&stat->start_time, 0, sizeof(stat->start_time));
			stat->active = false;
			stat->paused = false;
            memset(stat->custom_stats, 0, sizeof(stat->custom_stats));
        }
        return;
    }

    /* Full reset for non-distributed commands, but keep identity if requested. */
    free(logger_state.stats);
    logger_state.stats = NULL;
    logger_state.num_timers = 0;
    logger_state.in_distributed_xact = false;

    if (!logger_state.identity_persist && logger_state.identity != NULL)
    {
        free(logger_state.identity);
        logger_state.identity = NULL;
    }

	logger_state.command_tag[0] = '\0';
	logger_state.has_command_tag = false;

    ResetQueryActiveWallState();
    logger_init(_NUM_TIMING_SPOTS, timing_spot_names);
}

void log_message_internal(const char *file, int line, const char *format, ...)
{
    StringInfoData buf;
    va_list args;

    initStringInfo(&buf);

    va_start(args, format);
    appendStringInfoVA(&buf, format, args);
    va_end(args);

#ifndef FRONTEND
    bool need_backend_log_lock = (!redirection_done && MyBackendType != B_LOGGER);

    /*
     * Original code serialized every backend debug log message with
     * logger_shared->lock. Keep that fallback only when stderr is not using
     * the syslogger pipe, because the normal syslogger path already keeps a
     * single message intact.
     */
    /* if (logger_shared != NULL)
        LWLockAcquire(&logger_shared->lock, LW_EXCLUSIVE); */
    if (need_backend_log_lock && logger_shared != NULL)
        LWLockAcquire(&logger_shared->lock, LW_EXCLUSIVE);

    /* elog automatically adds timestamp. */
    elog(LOG, "[%s:%d] %s", file, line, buf.data);

    if (need_backend_log_lock && logger_shared != NULL)
        LWLockRelease(&logger_shared->lock);
    /* if (logger_shared != NULL)
        LWLockRelease(&logger_shared->lock); */
#else
    if (pthread_mutex_lock(&logger_print_mutex) == 0)
    {
        // Get current time for the log message timestamp
        char time_buf[32];
        struct timeval tv;
        gettimeofday(&tv, NULL);
        strftime(time_buf, sizeof(time_buf) - 1, "%Y-%m-%d %H:%M:%S", localtime(&tv.tv_sec));

        // Add milliseconds
        int len = strlen(time_buf);
        snprintf(time_buf + len, sizeof(time_buf) - len, ".%03ld", tv.tv_usec / 1000);

        // Print the file, line, timestamp, and the user's message
        // printf("[%s:%d] [%s] %s\n", file, line, time_buf, buf.data);
        fprintf(stderr, "[%s:%d] [%s] %s\n", file, line, time_buf, buf.data);
        fflush(stderr); // flush stderr immediately to avoid interleaving in frontend clients

        pthread_mutex_unlock(&logger_print_mutex);
    }
#endif

    pfree(buf.data);
}
