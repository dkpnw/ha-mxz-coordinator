"""A setup-era or uncompleted barrier cannot certify later fan delivery."""

import asyncio

from tools.issue25.test_restore import completed_fan


def call(number=0, token="middle"):
    entered, returned = asyncio.Event(), asyncio.Event()
    entered.set()
    returned.set()
    return {"number": number, "service": "fan", "args": {"fan_mode": token},
            "entered": entered, "returned": returned, "at": 10, "returned_at": 11}


def test_new_handler_and_arguments_required():
    old, new = call(), call(1)
    assert completed_fan([old, new], 1, "middle")
    assert not completed_fan([old], 1, "middle")
    assert not completed_fan([old, new], 1, "high")
    new["returned"].clear()
    assert not completed_fan([old, new], 1, "middle")


def test_stale_gate_and_timestamp_reject():
    old, new = call(), call(1)
    new["entered"] = old["entered"]
    assert not completed_fan([old, new], 1, "middle")
    new = call(1)
    new["returned_at"] = 9
    assert not completed_fan([old, new], 1, "middle")
    new = call(1)
    new["returned"] = new["entered"]
    assert not completed_fan([old, new], 1, "middle")
