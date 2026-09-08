"""`.env.example` against the settings model it claims to be generated from.

The decisions log asserted for months that the two "cannot drift". Nothing enforced
it, and they had: forty fields, thirty-six documented, with FLOODLINE_DATABASE_URL
missing - the single setting a deployment most needs. These tests are the enforcement
the sentence was describing.
"""

from __future__ import annotations

import re

from floodline.env_template import ENV_EXAMPLE, render_env_example
from floodline.settings import Settings


def test_the_committed_file_matches_the_model() -> None:
    """The whole point. Run `floodline env-template --write` when this fails."""
    assert ENV_EXAMPLE.exists(), f"{ENV_EXAMPLE} is missing"
    assert ENV_EXAMPLE.read_text() == render_env_example(), (
        "`.env.example` is out of date with floodline.settings.Settings. "
        "Regenerate it: floodline env-template --write"
    )


def test_every_setting_is_documented() -> None:
    """Stated separately from the byte comparison so a failure says which setting.

    An equality assertion on a 190-line file reports that two long strings differ. A
    deployer who cannot find a setting needs its name.
    """
    documented = set(re.findall(r"^(FLOODLINE_[A-Z0-9_]+)=", render_env_example(), re.M))
    declared = {f"FLOODLINE_{name.upper()}" for name in Settings.model_fields}
    assert declared - documented == set(), "settings absent from .env.example"
    assert documented - declared == set(), "documented settings that no longer exist"


def test_the_defaults_written_out_are_the_defaults_in_force() -> None:
    """A template that documents a value the code does not use is a trap.

    Someone copies it to `.env`, changes nothing, and silently pins an old default.
    """
    rendered = dict(re.findall(r"^(FLOODLINE_[A-Z0-9_]+)=(.*)$", render_env_example(), re.M))
    for name, field in Settings.model_fields.items():
        assert rendered[f"FLOODLINE_{name.upper()}"] == str(field.default), name


def test_the_compose_credentials_have_no_default_password() -> None:
    """`POSTGRES_PASSWORD=` with nothing after it, so compose refuses rather than
    starting a database on a password published in this repository."""
    text = ENV_EXAMPLE.read_text()
    assert "POSTGRES_PASSWORD=\n" in text, "a default password must not be shipped"
