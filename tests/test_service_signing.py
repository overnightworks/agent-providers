"""The service signs with one RSA-3072 key it only reads, and refuses a key it cannot trust."""

from __future__ import annotations

import base64
import json
import logging
import os
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed25519, rsa
from jwt.algorithms import RSAAlgorithm
from service_support import generated_rsa_key, pem_of, written_key_file
from test_service_config import VALID_CONFIG, _written
from webauth.jwt_verification import JwtVerifier, Rejected, RejectionReason, VerifiedClaims

from agent_service.cli import EXIT_BAD_CONFIG, main
from agent_service.config import ServiceConfig, load_config
from agent_service.signing import (
    EmptyTokenType,
    SigningKey,
    UnsoundSigningKey,
    key_id,
    load_signing_key,
)

RFC_7638_MODULUS = (
    "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7aPFFxuhDR1"
    "L6tSoc_BJECPebWKRXjBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0h4"
    "QyQ5v-65YGjQR0_FDW2QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5hajrn1n91CbO"
    "pbISD08qNLyrdkt-bFTWhAI4vMQFh6WeZu0fM4lFd2NcRwr3XPksINHaQ-G_xBniIqbw0Ls1jF44-csF"
    "Cur-kEgU8awapJzKnqDKgw"
)
RFC_7638_THUMBPRINT = "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"
ISSUER = "https://issuer.test"
KEY_SET_URL = "https://issuer.test/keys"
EXAMPLE_TOKEN_TYPE = "example+jwt"
PKCS8_OF_UNKNOWN_ALGORITHM = bytes.fromhex("300f020100300706032a03040500040100")


def _pem_of_unknown_algorithm() -> bytes:
    begin, *_, end = pem_of(generated_rsa_key()).decode().splitlines()
    body = base64.encodebytes(PKCS8_OF_UNKNOWN_ALGORITHM).decode()
    return f"{begin}\n{body}{end}\n".encode()


def _config_text(key_file: str) -> str:
    return f"{VALID_CONFIG}\n[signing]\nkey_file = {json.dumps(key_file)}\n"


def _loaded(tmp_path: Path, key_file: Path) -> ServiceConfig:
    return load_config(_written(tmp_path, _config_text(str(key_file))))


def _file_with(tmp_path: Path, contents: bytes, mode: int = 0o600) -> Path:
    path = tmp_path / "signing-key.pem"
    path.write_bytes(contents)
    path.chmod(mode)
    return path


def _key_file_of(key) -> Callable[[Path], str]:
    return lambda tmp_path: str(written_key_file(tmp_path, key()))


def _key_file_with_mode(mode: int) -> Callable[[Path], str]:
    return lambda tmp_path: str(written_key_file(tmp_path, generated_rsa_key(), mode))


def _file_holding(contents: Callable[[], bytes]) -> Callable[[Path], str]:
    return lambda tmp_path: str(_file_with(tmp_path, contents()))


def _directory(tmp_path: Path) -> str:
    return str(tmp_path)


def _fifo(tmp_path: Path) -> str:
    path = tmp_path / "signing-key.fifo"
    os.mkfifo(path, 0o600)
    return str(path)


def _symlink_to_mode(mode: int) -> Callable[[Path], str]:
    def arranged(tmp_path: Path) -> str:
        target = written_key_file(tmp_path, generated_rsa_key(), mode)
        link = tmp_path / "link.pem"
        link.symlink_to(target)
        return str(link)

    return arranged


def _unreadable(tmp_path: Path) -> str:
    return str(written_key_file(tmp_path, generated_rsa_key(), 0o000))


def _encrypted() -> bytes:
    return generated_rsa_key().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"passphrase"),
    )


def _public_key_pem() -> bytes:
    return (
        generated_rsa_key()
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )


@pytest.fixture(autouse=True)
def service_never_starts(monkeypatch):
    def refuse_to_serve(*_arguments, **_keywords):
        raise AssertionError("the service started with a key it should have refused")

    monkeypatch.setattr(uvicorn, "run", refuse_to_serve)


ABSOLUTE_PATH_REASON = "must be an absolute path to the signing key file; ~ is not expanded"
NOT_REGULAR = "is not a regular file"
NOT_A_PEM_KEY = "is not an unencrypted PEM private key"
UNSHOWABLE = "contains a control or non-printing character; a path must not"
BOUND_FOR_A_REFUSAL_SECONDS = 10


def _open_to_others(mode: int) -> str:
    return (
        f"is open to group or others (mode {mode:04o});"
        " a key file must grant them nothing, for example 0600 or 0400"
    )


RUNS_AS_ROOT = os.geteuid() == 0

REFUSED_KEY_FILES = [
    pytest.param(lambda _: "secrets/key.pem", ABSOLUTE_PATH_REASON, id="relative-path"),
    pytest.param(lambda _: "~/.secrets/key.pem", ABSOLUTE_PATH_REASON, id="home-relative-path"),
    pytest.param(
        lambda tmp_path: str(tmp_path / "absent.pem"),
        "cannot be read: No such file or directory",
        id="missing",
    ),
    pytest.param(
        _unreadable,
        "cannot be read: Permission denied",
        id="mode-0000",
        marks=pytest.mark.skipif(RUNS_AS_ROOT, reason="root reads any file"),
    ),
    pytest.param(lambda _: "/virtual/line\nbreak.pem", UNSHOWABLE, id="newline-in-path"),
    pytest.param(lambda _: "/virtual/nul\x00byte.pem", UNSHOWABLE, id="nul-in-path"),
    pytest.param(lambda _: "/virtual/escape\x1b[0m.pem", UNSHOWABLE, id="escape-in-path"),
    pytest.param(_directory, NOT_REGULAR, id="directory"),
    pytest.param(_fifo, NOT_REGULAR, id="fifo"),
    pytest.param(lambda _: "/dev/null", NOT_REGULAR, id="dev-null"),
    pytest.param(_key_file_with_mode(0o640), _open_to_others(0o640), id="mode-0640"),
    pytest.param(_key_file_with_mode(0o604), _open_to_others(0o604), id="mode-0604"),
    pytest.param(_key_file_with_mode(0o644), _open_to_others(0o644), id="mode-0644"),
    pytest.param(_symlink_to_mode(0o644), _open_to_others(0o644), id="symlink-to-0644"),
    pytest.param(
        _file_holding(lambda: b"A" * (16 * 1024 + 1)),
        "is larger than 16384 bytes",
        id="over-16-kib",
    ),
    pytest.param(
        _file_holding(lambda: pem_of(generated_rsa_key()) * 2),
        "holds more than one PEM block; it must hold exactly one",
        id="two-keys",
    ),
    pytest.param(
        _file_holding(_encrypted), "is encrypted; it must be an unencrypted PEM", id="encrypted"
    ),
    pytest.param(_file_holding(lambda: b"not a key"), NOT_A_PEM_KEY, id="garbage"),
    pytest.param(_file_holding(_public_key_pem), NOT_A_PEM_KEY, id="public-key"),
    pytest.param(_file_holding(lambda: b""), NOT_A_PEM_KEY, id="empty"),
    pytest.param(
        _file_holding(_pem_of_unknown_algorithm),
        "uses a key algorithm the service does not support",
        id="unknown-algorithm-oid",
    ),
    pytest.param(
        _key_file_of(lambda: ec.generate_private_key(ec.SECP256R1())),
        "is not an RSA key",
        id="ec-p256",
    ),
    pytest.param(
        _key_file_of(ed25519.Ed25519PrivateKey.generate), "is not an RSA key", id="ed25519"
    ),
    pytest.param(
        _key_file_of(lambda: dsa.generate_private_key(key_size=2048)), "is not an RSA key", id="dsa"
    ),
    pytest.param(
        _key_file_of(lambda: generated_rsa_key(2048)),
        "is an RSA key of 2048 bits; it must have 3072",
        id="rsa-2048",
    ),
    pytest.param(
        _key_file_of(lambda: generated_rsa_key(4096)),
        "is an RSA key of 4096 bits; it must have 3072",
        id="rsa-4096",
    ),
    pytest.param(
        _key_file_of(lambda: generated_rsa_key(3072, 3)),
        "has public exponent 3; it must be 65537",
        id="exponent-3",
    ),
]


def _main_within_bound(argv: list[str]) -> int:
    outcome: list[int] = []
    worker = threading.Thread(target=lambda: outcome.append(main(argv)), daemon=True)
    worker.start()
    worker.join(BOUND_FOR_A_REFUSAL_SECONDS)
    assert not worker.is_alive(), "startup hung instead of refusing"
    return outcome[0]


@pytest.mark.parametrize(("arrange", "reason"), REFUSED_KEY_FILES)
def test_an_unsound_key_file_stops_startup_with_one_line_naming_its_path_and_no_key_material(
    tmp_path, capsys, arrange, reason
):
    key_file = arrange(tmp_path)
    config_file = _written(tmp_path, _config_text(key_file))
    shown_key_file = key_file.encode("unicode_escape").decode("ascii")

    exit_status = _main_within_bound(["--config", str(config_file)])

    assert exit_status == EXIT_BAD_CONFIG
    assert capsys.readouterr().err == (
        f"agent-service: refusing to start: {config_file}: signing.key_file: "
        f"{shown_key_file} {reason}\n"
    )


def test_a_missing_key_file_is_not_created_by_the_service(tmp_path):
    missing = tmp_path / "absent.pem"

    main(["--config", str(_written(tmp_path, _config_text(str(missing))))])

    assert not missing.exists()


@pytest.mark.parametrize("mode", [0o600, 0o400, 0o700])
def test_a_key_file_granting_nothing_to_others_loads(tmp_path, mode):
    config = _loaded(tmp_path, written_key_file(tmp_path, generated_rsa_key(), mode))

    assert len(config.signing.key.kid) == 43


def test_a_symlink_to_a_private_key_file_loads(tmp_path):
    config = _loaded(tmp_path, Path(_symlink_to_mode(0o600)(tmp_path)))

    assert config.signing.key.kid == key_id(generated_rsa_key().public_key())


def test_a_path_swapped_after_the_open_still_loads_the_opened_file(tmp_path, monkeypatch):
    key_file = written_key_file(tmp_path, generated_rsa_key())
    real_open = os.open

    def open_then_swap(path, flags, *arguments):
        descriptor = real_open(path, flags, *arguments)
        impostor = tmp_path / "impostor.pem"
        impostor.write_bytes(b"garbage")
        impostor.chmod(0o644)
        impostor.rename(key_file)
        return descriptor

    monkeypatch.setattr(os, "open", open_then_swap)

    key = load_signing_key(key_file)

    assert key.kid == key_id(generated_rsa_key().public_key())


def _descriptors_reporting_size(monkeypatch, size: int) -> None:
    real_fstat = os.fstat

    def reporting(descriptor):
        reported = list(real_fstat(descriptor))
        reported[stat.ST_SIZE] = size
        return os.stat_result(reported)

    monkeypatch.setattr(os, "fstat", reporting)


def test_a_file_that_outgrows_its_reported_size_is_never_read_whole(tmp_path, monkeypatch):
    oversized = _file_with(tmp_path, b"A" * (64 * 1024))
    _descriptors_reporting_size(monkeypatch, 0)
    bytes_read = []
    real_read = os.read

    def counting_read(descriptor, length):
        chunk = real_read(descriptor, length)
        bytes_read.append(len(chunk))
        return chunk

    monkeypatch.setattr(os, "read", counting_read)

    with pytest.raises(UnsoundSigningKey, match="larger than"):
        load_signing_key(oversized)

    assert sum(bytes_read) == 16 * 1024 + 1


def test_a_descriptor_reporting_an_oversized_file_is_refused_before_reading(tmp_path, monkeypatch):
    key_file = written_key_file(tmp_path, generated_rsa_key())
    _descriptors_reporting_size(monkeypatch, 1 << 30)

    with pytest.raises(UnsoundSigningKey, match="larger than"):
        load_signing_key(key_file)


def test_a_key_file_is_read_once_so_overwriting_it_afterwards_changes_nothing(tmp_path):
    key_file = written_key_file(tmp_path, generated_rsa_key())
    config = _loaded(tmp_path, key_file)
    held_kid = config.signing.key.kid

    key_file.write_bytes(pem_of(generated_rsa_key(2048)))

    assert config.signing.key.kid == held_kid


def test_the_key_id_of_the_rfc_7638_example_key_is_its_published_thumbprint():
    modulus = int.from_bytes(base64.urlsafe_b64decode(RFC_7638_MODULUS + "=="), "big")
    public_key = rsa.RSAPublicNumbers(e=65537, n=modulus).public_key()

    assert key_id(public_key) == RFC_7638_THUMBPRINT


def test_a_key_file_gives_the_same_id_every_time_and_two_keys_give_two_ids(tmp_path):
    key_file = written_key_file(tmp_path, generated_rsa_key())
    other = SigningKey.from_private_key(rsa.generate_private_key(65537, 3072))

    first_load = load_signing_key(key_file)
    second_load = load_signing_key(key_file)

    assert first_load.kid == second_load.kid
    assert first_load.kid != other.kid


def _verifier_trusting(*published_keys: tuple[str, rsa.RSAPublicKey]) -> JwtVerifier:
    key_set = {
        "keys": [
            {**RSAAlgorithm.to_jwk(public, as_dict=True), "kid": kid, "use": "sig", "alg": "RS256"}
            for kid, public in published_keys
        ]
    }
    return JwtVerifier(
        issuer=ISSUER,
        audiences=["audience"],
        jwks_url=KEY_SET_URL,
        leeway_seconds=0,
        fetch_key_set=lambda _url: json.dumps(key_set).encode(),
        wall_clock=time.time,
        monotonic_clock=time.monotonic,
    )


def _claims() -> dict[str, object]:
    now = int(time.time())
    return {"iss": ISSUER, "aud": "audience", "sub": "s", "iat": now, "exp": now + 60}


def _signing_key(tmp_path: Path) -> SigningKey:
    return load_signing_key(written_key_file(tmp_path, generated_rsa_key()))


def _header_of(token: str) -> dict[str, object]:
    header = token.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))


@pytest.mark.parametrize("token_type", [EXAMPLE_TOKEN_TYPE, "other+jwt"])
def test_a_signed_token_has_the_typed_header_and_verifies_with_its_claims_unchanged(
    tmp_path, token_type
):
    key = _signing_key(tmp_path)
    claims = _claims()

    token = key.sign(claims, token_type)

    verified = _verifier_trusting((key.kid, key.public_key)).verify(token)
    assert _header_of(token) == {"alg": "RS256", "kid": key.kid, "typ": token_type}
    assert isinstance(verified, VerifiedClaims)
    assert dict(verified.claims) == claims


def test_a_token_without_a_type_is_refused(tmp_path):
    key = _signing_key(tmp_path)
    claims = _claims()

    with pytest.raises(EmptyTokenType):
        key.sign(claims, "")


def test_a_token_does_not_verify_under_another_key_published_with_the_same_id(tmp_path):
    key = _signing_key(tmp_path)
    impostor = rsa.generate_private_key(65537, 3072).public_key()

    verdict = _verifier_trusting((key.kid, impostor)).verify(
        key.sign(_claims(), EXAMPLE_TOKEN_TYPE)
    )

    assert verdict == Rejected(RejectionReason.INVALID_SIGNATURE)


def test_no_representation_of_the_key_shows_key_material(tmp_path):
    config = _loaded(tmp_path, written_key_file(tmp_path, generated_rsa_key()))
    key = config.signing.key
    secret_lines = pem_of(generated_rsa_key()).decode().splitlines()

    shown = [
        repr(key),
        str(key),
        repr(config.signing),
        str(config.signing),
        repr(config),
        str(config),
    ]

    assert repr(key) == f"SigningKey(kid={key.kid!r})"
    assert all(key.kid in text for text in shown)
    assert not any("object at" in text for text in shown)
    assert not any(line in text for text in shown for line in secret_lines)
    assert not any("PRIVATE KEY" in text for text in shown)


def test_loading_and_signing_log_no_key_material(tmp_path, caplog):
    key_file = written_key_file(tmp_path, generated_rsa_key())
    secret_lines = pem_of(generated_rsa_key()).decode().splitlines()

    with caplog.at_level(logging.DEBUG):
        _loaded(tmp_path, key_file).signing.key.sign(_claims(), EXAMPLE_TOKEN_TYPE)

    formatted = [logging.Formatter().format(record) for record in caplog.records]
    assert not any(line in text for text in formatted for line in secret_lines)
