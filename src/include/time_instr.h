/**
 * --- USAGE ---
 * The API for timing remains the same. The log_message function is now a macro,
 * but is called in the exact same way.
 *
 * // In any file:
 * #include "dbcomm_time_instr.h"
 * remember to call logger_init() once at the beginning somewhere
 * Then, to time a code section: timing_start(TIMER_ID); ...
 * timing_end(TIMER_ID); Finally, to print the report: logger_print_timings();
 *
 * // note: logging a single message like so
 * log_message("Starting process with value %d...", 42);
 * // Output will now include file and line number.
 */
#pragma once
#ifndef LOGGER_H
#define LOGGER_H

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <time.h>

#include "latency_instr.h"
#include "./timing_spots.h"

// --- API ---

/**
 * @brief Initializes the logging and timing system.
 *
 * This function must be called once before any other logger or timing function.
 * It allocates the necessary internal structures.
 *
 * @param num_timers The total number of timers, typically from the user-defined enum (_NUM_TIMERS).
 * @param names An array of strings containing the names for each timer ID.
 * @return 0 on success, -1 on failure (e.g., memory allocation failed).
 */
int logger_init(int num_timers, const char *names[]);

/**
 * @brief Records the start time for a specific timer ID.
 *
 * This function is designed to be very fast and does not perform any memory allocations.
 * It uses clock_gettime(CLOCK_MONOTONIC_RAW).
 *
 * @param timer_id The enum value of the timer to start.
 */
void timing_start(int timer_id);

/**
 * @brief Records the end time for a specific timer ID and accumulates the result.
 *
 * This function calculates the elapsed time since the corresponding timing_start() call
 * and adds it to the total for that timer ID, also incrementing the call count.
 *
 * @param timer_id The enum value of the timer to end.
 */
void timing_end(int timer_id);

/**
 * @brief Pauses a running timer without incrementing its call count.
 *
 * This is intended for leaf timers that need to exclude nested wait/child work
 * while keeping the outer logical call counted only once.
 *
 * @param timer_id The enum value of the timer to pause.
 */
void timing_pause(int timer_id);

/**
 * @brief Resumes a previously paused timer.
 *
 * This restarts timing for the same logical call after timing_pause().
 *
 * @param timer_id The enum value of the timer to resume.
 */
void timing_resume(int timer_id);

/**
 * @brief Adds a value to a custom statistic for a specific timer.
 *
 * This function allows tracking additional metrics beyond just timing,
 * such as bytes processed, bytes sent, etc. The stat is identified by
 * an enum key (defined in custom_stats.h), and the value is accumulated.
 *
 * Example usage:
 *   timing_add_stat(TIMER_NETWORK, STAT_TOTAL_BYTES_PROCESSED, 1024);
 *   timing_add_stat(TIMER_NETWORK, STAT_TOTAL_BYTES_SENT, 512);
 *
 * @param timer_id The enum value of the timer to add the stat to.
 * @param stat_key The custom_stat_key_t enum value for the stat.
 * @param value The value to add to the stat (accumulated across calls).
 */
void timing_add_stat(int timer_id, int stat_key, uint64_t value);

/**
 * @brief Prints a formatted report of timing measurements and performs a reset.
 *
 * When a distributed transaction is active (see logger_set_distributed_xact_state),
 * this function only prints non-transactional timers and only resets those.
 * Otherwise it prints and resets all timers.
 */
void logger_print_timings(void);

void logger_cleanup();

/**
 * @brief Resets timing measurements based on distributed transaction state.
 *
 * If a distributed transaction is active, only non-transactional timers are
 * cleared so transactional timers can span multiple commands. Otherwise all
 * timers are cleared and reinitialized.
 */
void logger_reset();

/**
 * @brief Start the top-level active execution wall timer for the current query cycle.
 *
 * This timer is the primary denominator for communication-stack percentages.
 * It spans the logical query cycle on this backend and is paused around waits
 * via logger_query_active_wall_pause_wait()/resume_wait().
 */
void logger_query_active_wall_start(void);

/**
 * @brief Stop the current query-cycle active execution wall timer.
 *
 * This finalizes QUERY_ACTIVE_WALL for the just-finished logical query cycle.
 * If the timer is currently paused inside a wait, the helper closes the wait
 * bucket first and then ends the active-wall timer.
 */
void logger_query_active_wall_stop(void);

/**
 * @brief Pause the current query-cycle active wall timer because the backend is waiting.
 *
 * Nested waits are tracked with a small depth counter so the top-level timer is
 * only paused once until the outermost wait finishes. QUERY_WAIT_WALL is the
 * matching optional/debug bucket that accumulates the excluded wait time.
 */
void logger_query_active_wall_pause_wait(void);

/**
 * @brief Resume the current query-cycle active wall timer after a wait ends.
 *
 * This is the counterpart to logger_query_active_wall_pause_wait().
 */
void logger_query_active_wall_resume_wait(void);

/**
 * @brief Return true if a logical query-cycle active wall timer is currently open.
 */
bool logger_query_active_wall_is_running(void);

/**
 * @brief Marks whether the logger is inside a distributed transaction.
 *
 * When set to true, logger_print_timings and logger_reset preserve transactional
 * timer state across commands so XACT_* timers can span BEGIN/COMMIT.
 *
 * @param in_distributed_xact true when a distributed transaction is active.
 */
void logger_set_distributed_xact_state(bool in_distributed_xact);

/**
 * @brief Sets an identity string to associate timing reports with a transaction.
 *
 * Passing NULL clears the identity. This is used to tag timing reports with a
 * distributed transaction id when available, and is a no-op for non-transactional
 * logging.
 *
 * @param identity A string to copy, or NULL to clear.
 */
void logger_set_identity(const char *identity);

/**
 * @brief Controls whether the current identity should persist across commands.
 *
 * When set to true, logger_reset keeps the identity even when not in a
 * distributed transaction, allowing explicit BEGIN/COMMIT blocks to retain
 * the identity across multiple commands.
 *
 * @param persist true to keep identity across commands, false to clear on reset.
 */
void logger_set_identity_persist(bool persist);
bool logger_identity_persist_enabled(void);
bool logger_distributed_xact_active(void);
const char * logger_get_identity(void);
void logger_set_command_tag(const char *commandTag);
const char * logger_get_command_tag(void);

#ifndef FRONTEND
size_t LoggerShmemSize(void);
void LoggerShmemInit(void);
#endif

/**
 * @brief The internal implementation of the logger. Do not call this directly.
 * Use the log_message() macro instead.
 */
void log_message_internal(const char *file, int line, const char *format, ...);

/**
 * @brief Logs a general-purpose message, similar to printf.
 *
 * This is a macro that automatically captures the file name and line number.
 * It prepends the file, line, and a wall-clock timestamp to the message.
 *
 * @param format The format string.
 * @param ... Variadic arguments for the format string.
 */
#define log_message(format, ...) log_message_internal(__FILE__, __LINE__, format, ##__VA_ARGS__)

/**
 * @brief Backend-only debug log helper that also marks instrumentation overhead.
 *
 * The waterfall compresses these spans out of the displayed transaction
 * timeline so our own debug logging does not inflate command lifecycles.
 */
#ifndef FRONTEND
#define log_message_traced(format, ...)                                         \
	do                                                                          \
	{                                                                           \
		LatencyTraceHandle _backend_logging_handle =                            \
			latency_trace_begin(LATENCY_STAGE_BACKEND_LOGGING, 0, 0);            \
		log_message(format, ##__VA_ARGS__);                                     \
		latency_trace_end(_backend_logging_handle);                             \
	} while (0)
#else
#define log_message_traced(format, ...) log_message(format, ##__VA_ARGS__)
#endif

#endif // LOGGER_H
