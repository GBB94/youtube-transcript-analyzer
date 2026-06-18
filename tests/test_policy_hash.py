"""policy_hash is a cache-key input. If a knob can change the *result* for the
same input, changing it must change the hash — otherwise we serve stale results
across config changes (see CLAUDE.md, cache contract)."""
from transcript_tool.policy import EgressPolicy, Policy, QualityConfig


def test_policy_hash_is_stable_for_identical_policies():
    assert Policy().policy_hash() == Policy().policy_hash()


def test_language_preference_changes_hash():
    assert Policy(languages=("en",)).policy_hash() != Policy(languages=("es",)).policy_hash()
    # Order matters: it is a preference list, not a set.
    assert (
        Policy(languages=("en", "es")).policy_hash()
        != Policy(languages=("es", "en")).policy_hash()
    )


def test_enabled_strategies_change_hash():
    base = Policy(enabled_strategies=("uploaded_caption",))
    more = Policy(enabled_strategies=("uploaded_caption", "api_captions"))
    assert base.policy_hash() != more.policy_hash()


def test_quality_config_changes_hash():
    base = Policy()
    tweaked = Policy(quality=QualityConfig(max_cps=10.0))
    assert base.policy_hash() != tweaked.policy_hash()
    # A different hard-gate setting must also move the hash.
    relaxed = Policy(quality=QualityConfig(require_monotonic_timestamps=False))
    assert base.policy_hash() != relaxed.policy_hash()


def test_egress_policy_changes_hash():
    base = Policy()
    networked = Policy(egress=EgressPolicy(allow_network=True))
    public = Policy(egress=EgressPolicy(allow_public_url=True))
    assert base.policy_hash() != networked.policy_hash()
    assert base.policy_hash() != public.policy_hash()
    assert networked.policy_hash() != public.policy_hash()
