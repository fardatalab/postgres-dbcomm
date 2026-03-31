/*
 * printtup_binary_dump_client.c
 *
 * Minimal libpq client used to drive PostgreSQL's binary result-row path when
 * validating the printtup binary dump instrumentation. Plain psql typically
 * consumes text-format results, so this helper explicitly requests binary
 * result columns with resultFormat=1.
 *
 * By default the client also enables single-row mode so large result sets can
 * be streamed without materializing the whole query result on the client.
 */

#include <libpq-fe.h>

#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct ClientOptions
{
	const char *conninfo;
	const char *query;
	bool		single_row_mode;
	long long	progress_every;
} ClientOptions;

/*
 * parse_ll_option
 *
 * Parse a strictly positive integer command-line argument for options such as
 * progress cadence. The dump workflow is debugging-oriented, so reject invalid
 * inputs loudly instead of silently guessing.
 */
static long long
parse_ll_option(const char *option_name, const char *value)
{
	char	   *endptr = NULL;
	long long	parsed = strtoll(value, &endptr, 10);

	if (value == NULL || value[0] == '\0' || endptr == value ||
		(endptr != NULL && *endptr != '\0') || parsed < 0)
	{
		fprintf(stderr, "invalid value for %s: %s\n",
				option_name, value != NULL ? value : "(null)");
		exit(1);
	}

	return parsed;
}

/*
 * print_usage
 *
 * Keep the usage text close to the supported flags. This helper is primarily
 * for local experimentation, so the interface is intentionally small.
 */
static void
print_usage(const char *argv0)
{
	fprintf(stderr,
			"usage: %s [--conninfo CONNINFO] [--query SQL] "
			"[--progress-every N] [--no-single-row-mode]\n",
			argv0);
	fprintf(stderr,
			"defaults: conninfo=\"dbname=tpch_sf10\", "
			"query=\"select * from lineitem limit 100000\"\n");
}

/*
 * parse_options
 *
 * Parse a small set of long-form options. This avoids pulling in getopt
 * variants and keeps the build command simple for quick experiments.
 */
static void
parse_options(int argc, char **argv, ClientOptions *options)
{
	int			argindex = 1;

	while (argindex < argc)
	{
		const char *arg = argv[argindex];

		if (strcmp(arg, "--conninfo") == 0)
		{
			if (argindex + 1 >= argc)
			{
				fprintf(stderr, "--conninfo requires an argument\n");
				print_usage(argv[0]);
				exit(1);
			}

			options->conninfo = argv[argindex + 1];
			argindex += 2;
		}
		else if (strcmp(arg, "--query") == 0)
		{
			if (argindex + 1 >= argc)
			{
				fprintf(stderr, "--query requires an argument\n");
				print_usage(argv[0]);
				exit(1);
			}

			options->query = argv[argindex + 1];
			argindex += 2;
		}
		else if (strcmp(arg, "--progress-every") == 0)
		{
			if (argindex + 1 >= argc)
			{
				fprintf(stderr, "--progress-every requires an argument\n");
				print_usage(argv[0]);
				exit(1);
			}

			options->progress_every =
				parse_ll_option("--progress-every", argv[argindex + 1]);
			argindex += 2;
		}
		else if (strcmp(arg, "--no-single-row-mode") == 0)
		{
			options->single_row_mode = false;
			argindex += 1;
		}
		else if (strcmp(arg, "--help") == 0)
		{
			print_usage(argv[0]);
			exit(0);
		}
		else
		{
			fprintf(stderr, "unrecognized argument: %s\n", arg);
			print_usage(argv[0]);
			exit(1);
		}
	}
}

/*
 * print_column_formats
 *
 * The dump instrumentation is binary-only. Emit the result format of every
 * column so it is obvious whether the client actually exercised the desired
 * printtup binary path.
 */
static void
print_column_formats(PGresult *res)
{
	int			nfields = PQnfields(res);
	int			fieldindex = 0;

	printf("cols=%d\n", nfields);

	for (fieldindex = 0; fieldindex < nfields; fieldindex++)
		printf("col=%d format=%d\n", fieldindex, PQfformat(res, fieldindex));
}

/*
 * run_single_row_query
 *
 * Stream the result one tuple at a time while still requesting binary column
 * format. This matches the large-result validation flow used for the dump.
 */
static int
run_single_row_query(PGconn *conn, const ClientOptions *options)
{
	PGresult   *res = NULL;
	long long	rowcount = 0;
	int			nfields = -1;
	bool		printed_formats = false;

	if (!PQsendQueryParams(conn, options->query, 0, NULL, NULL, NULL, NULL, 1))
	{
		fprintf(stderr, "send query failed: %s", PQerrorMessage(conn));
		return 1;
	}

	if (!PQsetSingleRowMode(conn))
	{
		fprintf(stderr, "failed to enable single-row mode\n");
		return 1;
	}

	while ((res = PQgetResult(conn)) != NULL)
	{
		ExecStatusType status = PQresultStatus(res);

		if (status == PGRES_SINGLE_TUPLE)
		{
			if (nfields < 0)
				nfields = PQnfields(res);

			if (!printed_formats)
			{
				print_column_formats(res);
				printed_formats = true;
			}

			rowcount++;
			if (options->progress_every > 0 &&
				(rowcount % options->progress_every) == 0)
			{
				printf("rows=%lld\n", rowcount);
				fflush(stdout);
			}
		}
		else if (status == PGRES_TUPLES_OK)
		{
			/* End-of-query marker for single-row mode. */
		}
		else
		{
			fprintf(stderr, "query failed: %s", PQerrorMessage(conn));
			PQclear(res);
			return 1;
		}

		PQclear(res);
	}

	printf("final_rows=%lld cols=%d\n", rowcount, nfields);
	return 0;
}

/*
 * run_materialized_query
 *
 * Keep a non-streaming fallback for small experiments where materializing the
 * result set is acceptable. This still requests binary columns with
 * PQexecParams(..., resultFormat=1).
 */
static int
run_materialized_query(PGconn *conn, const ClientOptions *options)
{
	PGresult   *res = NULL;

	res = PQexecParams(conn, options->query, 0, NULL, NULL, NULL, NULL, 1);
	if (PQresultStatus(res) != PGRES_TUPLES_OK)
	{
		fprintf(stderr, "query failed: %s", PQerrorMessage(conn));
		PQclear(res);
		return 1;
	}

	print_column_formats(res);
	printf("final_rows=%d cols=%d\n", PQntuples(res), PQnfields(res));
	PQclear(res);
	return 0;
}

int
main(int argc, char **argv)
{
	ClientOptions options =
	{
		.conninfo = "dbname=tpch_sf10",
		.query = "select * from lineitem limit 100000",
		.single_row_mode = true,
		.progress_every = 0
	};
	PGconn	   *conn = NULL;
	int			exitcode = 0;

	parse_options(argc, argv, &options);

	conn = PQconnectdb(options.conninfo);
	if (PQstatus(conn) != CONNECTION_OK)
	{
		fprintf(stderr, "connection failed: %s", PQerrorMessage(conn));
		PQfinish(conn);
		return 1;
	}

	printf("conninfo=%s\n", options.conninfo);
	printf("query=%s\n", options.query);
	printf("single_row_mode=%d\n", options.single_row_mode ? 1 : 0);
	printf("progress_every=%lld\n", options.progress_every);

	if (options.single_row_mode)
		exitcode = run_single_row_query(conn, &options);
	else
		exitcode = run_materialized_query(conn, &options);

	PQfinish(conn);
	return exitcode;
}
