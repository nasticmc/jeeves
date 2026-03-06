from __future__ import annotations

from meshcore_pathbot.core.bot import parse_rx_log_data


def test_parse_rx_log_data_compacts_three_byte_hashes_from_raw_payload() -> None:
    # header=0x00, path byte=0x83 => hash size=3 bytes, path_len=3
    parsed = parse_rx_log_data("0083a1b2c3d4e5f6070809")

    assert parsed["path_hash_size"] == 3
    assert parsed["path_len"] == 3
    assert parsed["path"] == "a1d407"
    assert parsed["full_path"] == "a1b2c3d4e5f6070809"


def test_parse_rx_log_data_uses_dict_payload_fields_when_available() -> None:
    parsed = parse_rx_log_data(
        {
            "path_len": 3,
            "path_hash_size": 2,
            "path": "a1b2c3d4e5f6",
        }
    )

    assert parsed["path_hash_size"] == 2
    assert parsed["path_len"] == 3
    assert parsed["path"] == "a1c3e5"
    assert parsed["full_path"] == "a1b2c3d4e5f6"
