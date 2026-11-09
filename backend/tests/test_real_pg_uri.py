"""The test harness must address its throwaway database on every platform."""

from tests.support.real_pg import database_uri


def test_tcp_form_changes_only_the_database():
    assert (
        database_uri("postgresql://postgres:@127.0.0.1:54321/postgres", "outly")
        == "postgresql://postgres:@127.0.0.1:54321/outly"
    )


def test_unix_socket_form_keeps_the_socket_directory():
    # Linux (and so GitHub Actions): the socket directory lives in the query, and
    # the old string-cutting turned "host=/tmp/outly_pg_x" into "host=/tmp/outly".
    assert (
        database_uri("postgresql://postgres:@/postgres?host=/tmp/outly_pg_x", "outly")
        == "postgresql://postgres:@/outly?host=/tmp/outly_pg_x"
    )


def test_a_url_a_driver_can_still_read_after_the_scheme_swap():
    swapped = "postgresql+psycopg://" + database_uri(
        "postgresql://postgres:@/postgres?host=/tmp/x", "outly"
    ).split("://", 1)[1]
    assert swapped == "postgresql+psycopg://postgres:@/outly?host=/tmp/x"
