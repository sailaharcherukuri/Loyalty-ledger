"""Atomic, zero-padded IDs for business records."""


def next_id(cursor, sequence_name, prefix, width):
    """Return the next ID using a database row as a concurrency-safe counter."""
    cursor.execute(
        """
        INSERT INTO id_sequences (sequence_name, sequence_value)
        VALUES (%s, 0)
        ON DUPLICATE KEY UPDATE sequence_name = VALUES(sequence_name)
        """,
        (sequence_name,),
    )
    cursor.execute(
        """
        UPDATE id_sequences
        SET sequence_value = LAST_INSERT_ID(sequence_value + 1)
        WHERE sequence_name = %s
        """,
        (sequence_name,),
    )
    cursor.execute("SELECT LAST_INSERT_ID()")
    number = cursor.fetchone()[0]
    return f"{prefix}-2026-{number:0{width}d}"
