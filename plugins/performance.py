from datasette import hookimpl

@hookimpl
def prepare_connection(conn, database):
    conn.execute("PRAGMA cache_size = -262144")   # 256 MB
    conn.execute("PRAGMA mmap_size = 2147483648")  # 2 GB
