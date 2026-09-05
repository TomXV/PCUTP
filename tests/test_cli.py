"""The CLI's own ergonomics: flag position, port resolution."""

import pytest

from pcutp import daemon


@pytest.mark.parametrize(
    "argv",
    [
        ["--trace", "serve", "--port", "/dev/null"],
        ["serve", "--trace", "--port", "/dev/null"],
    ],
)
def test_trace_accepted_on_either_side_of_the_subcommand(argv):
    args = daemon.build_parser().parse_args(argv)
    assert args.trace is True


def test_flag_before_subcommand_survives_being_omitted_after_it():
    args = daemon.build_parser().parse_args(["--block", "1024", "serve", "--port", "x"])
    assert args.block == 1024


def test_flag_after_subcommand_wins():
    argv = ["--block", "1024", "serve", "--block", "512", "--port", "x"]
    assert daemon.build_parser().parse_args(argv).block == 512


def test_port_defaults_to_auto():
    args = daemon.build_parser().parse_args(["serve"])
    assert args.port == "auto"


def test_explicit_port_is_passed_through():
    assert daemon.resolve_port("/dev/ttyUSB9") == "/dev/ttyUSB9"


def test_auto_with_no_adapters_explains_itself(monkeypatch):
    monkeypatch.setattr(daemon, "usb_ports", list)
    with pytest.raises(SystemExit, match="no USB serial adapter"):
        daemon.resolve_port("auto")


def test_auto_with_one_adapter_picks_it(monkeypatch):
    monkeypatch.setattr(daemon, "usb_ports", lambda: [("/dev/serial/by-id/x", "FTDI")])
    assert daemon.resolve_port("auto") == "/dev/serial/by-id/x"


def test_auto_with_several_adapters_lists_them(monkeypatch):
    monkeypatch.setattr(
        daemon, "usb_ports", lambda: [("/dev/a", "CH340"), ("/dev/b", "FTDI")]
    )
    with pytest.raises(SystemExit, match="/dev/a"):
        daemon.resolve_port("auto")


def test_console_keeps_the_bar_below_log_lines(capsys):
    console = daemon.Console()
    console.progress(50, 100)
    console.log("RX< ACK 3")
    out = capsys.readouterr().out
    assert "RX< ACK 3" in out
    assert out.rstrip().endswith("50 / 100 bytes")
