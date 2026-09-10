"""Whether a background feature runs, and where that was decided.

Three levels and two fields, resolved without a database: the same shape
test_schedule pins the indexing schedule with, plus the one rule that is not
the schedule's - the global switch is a gate rather than a default.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.test_storage import FakeCursor

from enggraph.config import FEATURE_EMBEDDING, FEATURE_KEY_TTL_DAYS, FEATURE_SUMMARIZE
from enggraph.features import Feature, resolve

PROJECT = "alpha"


def cursor(**levels: dict) -> FakeCursor:
    """Build a cursor holding one feature object per level, keyed as rows are."""
    objects = {}
    for key, value in levels.items():
        objects["_settings" if key == "global" else PROJECT] = value
    return FakeCursor(objects=objects)


def embedding(**levels: dict) -> Feature:
    """Resolve embedding over the levels given, which is what most tests ask."""
    seeded = cursor(
        **{key: {FEATURE_EMBEDDING: value} for key, value in levels.items()}
    )
    return resolve(seeded, PROJECT, FEATURE_EMBEDDING)


def test_a_feature_nobody_configured_takes_its_built_in_answer() -> None:
    """Embedding starts off: a queue must not fill itself on a fresh install."""
    settled = resolve(cursor(), PROJECT, FEATURE_EMBEDDING)
    assert settled.enabled is False
    assert settled.origins["enabled"] == "default"


def test_summarizing_starts_on_so_the_switch_changes_nothing_unused() -> None:
    """Today's behaviour is what an unset switch has to mean."""
    assert resolve(cursor(), PROJECT, FEATURE_SUMMARIZE).enabled is True


def test_the_global_switch_reaches_a_project_that_says_nothing() -> None:
    """One row turns embedding on for everything that has not overridden it."""
    settled = embedding(**{"global": {"enabled": True}})
    assert settled.enabled is True
    assert settled.origins["enabled"] == "global"


def test_a_project_turns_itself_on_under_a_global_that_is_silent() -> None:
    """The schedule's walk: the first level that states a field decides it."""
    settled = embedding(project={"enabled": True})
    assert settled.enabled is True
    assert settled.origins["enabled"] == "project"


def test_disabling_a_feature_beats_a_project_that_says_on() -> None:
    """The kill switch: disabled there is off everywhere, no exceptions.

    Without it, shutting the model down would mean visiting every project
    first, and an operator would have no way to be sure they had.
    """
    settled = embedding(
        **{"global": {"allowed": False, "enabled": True}, "project": {"enabled": True}}
    )
    assert settled.enabled is False
    assert settled.allowed is False
    assert settled.gated is True


def test_a_project_may_switch_itself_on_under_a_global_default_of_off() -> None:
    """The other half of the split, and the reason for it.

    `enabled` at the global level is what a project that says nothing does.
    A project that does say something is not overruled by it - otherwise
    turning a feature off by default would also forbid it, which is what a
    default is not.
    """
    settled = embedding(**{"global": {"enabled": False}, "project": {"enabled": True}})
    assert settled.enabled is True
    assert settled.allowed is True
    assert settled.gated is False
    assert settled.origins["enabled"] == "project"


def test_a_project_turning_itself_off_is_not_gated() -> None:
    """Gated names one case: the feature is disabled, not merely off here."""
    settled = embedding(project={"enabled": False})
    assert settled.enabled is False
    assert settled.gated is False


def test_a_stored_url_answers_before_the_environment() -> None:
    """Which is what makes the field in the dashboard worth having."""
    settled = embedding(
        **{"global": {"enabled": True, "server_url": "http://gpu:8080"}}
    )
    assert settled.server_url == "http://gpu:8080"
    assert settled.origins["server_url"] == "global"


def test_a_project_url_beats_the_global_one() -> None:
    """Fields resolve on their own, as every field of a schedule does."""
    settled = embedding(
        **{
            "global": {"enabled": True, "server_url": "http://gpu:8080"},
            "project": {"server_url": "https://other:9000"},
        }
    )
    assert settled.enabled is True
    assert settled.server_url == "https://other:9000"


def test_a_url_that_is_not_one_is_refused_rather_than_stored() -> None:
    """These rows are editable in psql, so the reader is the second guard."""
    settled = embedding(**{"global": {"enabled": True, "server_url": "gpu:8080"}})
    assert settled.server_url == ""
    assert settled.origins["server_url"] == "default"


def test_a_trailing_slash_is_not_part_of_the_address() -> None:
    """The path is appended to it, and two slashes are not the same route."""
    settled = embedding(
        **{"global": {"enabled": True, "server_url": "http://gpu:8080/"}}
    )
    assert settled.server_url == "http://gpu:8080"


def test_a_switch_that_is_not_a_boolean_is_ignored() -> None:
    """A string "true" is a typo, and acting on it would be a guess."""
    settled = embedding(**{"global": {"enabled": "yes"}})
    assert settled.enabled is False
    assert settled.origins["enabled"] == "default"


def saved(days_ago: int) -> str:
    """Return a key written this many days ago, as the dashboard stamps it."""
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def test_a_fresh_key_is_not_due_for_renewal() -> None:
    """The TTL is a rotation reminder, and a new key has not earned one."""
    settled = embedding(
        **{
            "global": {
                "enabled": True,
                "server_key": "secret",
                "key_saved_at": saved(1),
            }
        }
    )
    assert settled.server_key == "secret"
    assert settled.key_expired is False
    assert settled.key_due() is not None


def test_a_key_older_than_the_window_reads_as_expired() -> None:
    """Which is the red line beside the field, and nothing more than that."""
    settled = embedding(
        **{
            "global": {
                "enabled": True,
                "server_key": "secret",
                "key_saved_at": saved(FEATURE_KEY_TTL_DAYS + 1),
            }
        }
    )
    assert settled.key_expired is True
    # Still handed out: a queue that stopped itself because a date passed
    # would be the silent failure the switch exists to avoid.
    assert settled.server_key == "secret"


def test_no_key_is_never_expired() -> None:
    """An absent token has nothing to renew, so it says nothing."""
    settled = embedding(**{"global": {"enabled": True}})
    assert settled.server_key == ""
    assert settled.key_expired is False
    assert settled.key_due() is None


def test_a_date_that_is_not_one_leaves_the_key_alone() -> None:
    """These rows are editable in psql, and a typo must not revoke a token."""
    settled = embedding(
        **{
            "global": {
                "enabled": True,
                "server_key": "secret",
                "key_saved_at": "last tuesday",
            }
        }
    )
    assert settled.key_expired is False
    assert settled.server_key == "secret"


def test_a_project_key_beats_the_global_one() -> None:
    """One project may talk to a server of its own, key included."""
    settled = embedding(
        **{
            "global": {"enabled": True, "server_key": "shared"},
            "project": {"server_key": "mine"},
        }
    )
    assert settled.server_key == "mine"
    assert settled.origins["server_key"] == "project"
