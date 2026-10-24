"""Scorer identity and estimate-cache attacks.

Equivalent to the harness-identity findings against ``source_identity`` (a copied
sha256, a newline in that sha, and a ``core`` subclass outside the hashed file
set). The optical-flow adapter stores bins in ``EstimateCache``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from redteam.scorers import (  # noqa: E402
    EstimateCache,
    MotionRuleV3Scorer,
    NoClaimPanEstimator,
    PanAgreementScorer,
    estimate_cache_key,
    frame_source_identity,
    source_identity,
)

ROOT = Path(__file__).resolve().parents[1]
HEX64 = "ab" * 32


def test_subclass_outside_the_default_file_set_changes_identity():
    base = PanAgreementScorer(NoClaimPanEstimator()).identity()

    class Sub(NoClaimPanEstimator):
        pass

    sub = PanAgreementScorer(Sub()).identity()
    rel = Path(__file__).resolve().relative_to(ROOT).as_posix()
    assert sub.sha256 != base.sha256
    assert sub.sha256 != "unhashed"
    assert rel in sub.files
    assert rel not in base.files


def test_two_core_classes_in_one_file_do_not_share_an_identity():
    class One(NoClaimPanEstimator):
        pass

    class Two(NoClaimPanEstimator):
        def estimate(self, sample):
            return []

    one = source_identity(PanAgreementScorer(One()), One())
    two = source_identity(PanAgreementScorer(Two()), Two())
    assert one.sha256 != two.sha256
    assert one.estimator_sha256 is None
    assert two.estimator_sha256 is None


def test_copied_sha_is_not_stored_as_estimator_sha256():
    stolen = "cd" * 32

    class Spoof(NoClaimPanEstimator):
        sha256 = stolen

    ident = source_identity(PanAgreementScorer(Spoof()), Spoof())
    plain = PanAgreementScorer(NoClaimPanEstimator()).identity()
    assert ident.estimator_sha256 is None
    assert ident.sha256 != "unhashed"
    assert ident.sha256 != plain.sha256
    assert stolen not in repr(ident)


def test_newline_or_wrong_length_sha_is_not_stored():
    class Weird(NoClaimPanEstimator):
        sha256 = "x\nestimator=forged"

    ident = source_identity(PanAgreementScorer(Weird()), Weird())
    assert ident.estimator_sha256 is None
    assert ident.sha256 != "unhashed"
    assert "estimator=forged" not in repr(ident)
    assert all("\n" not in item for item in ident.files)

    class Short(NoClaimPanEstimator):
        sha256 = "ab" * 31

    class Upper(NoClaimPanEstimator):
        sha256 = "AB" * 32

    class AsStr(str):
        pass

    class Child(NoClaimPanEstimator):
        sha256 = AsStr(HEX64)

    for cls, secret in ((Short, "ab" * 31), (Upper, "AB" * 32), (Child, HEX64)):
        bad = source_identity(PanAgreementScorer(cls()), cls())
        assert bad.estimator_sha256 is None
        assert bad.sha256 != "unhashed"
        assert secret not in repr(bad)


def test_property_sha_is_not_trusted():
    class Computed(NoClaimPanEstimator):
        @property
        def sha256(self):
            return HEX64

        @property
        def hash(self):
            return "12" * 32

    ident = source_identity(PanAgreementScorer(Computed()), Computed())
    assert ident.estimator_sha256 is None
    assert HEX64 not in repr(ident)
    assert "12" * 32 not in repr(ident)


def test_forged_module_and_qualname_do_not_take_the_real_identity():
    from qor_receipt.vision.pan_estimator import OpticalFlowPanEstimator, PanParams, estimator_sha256

    class Fake(NoClaimPanEstimator):
        @property
        def sha256(self):
            return estimator_sha256()

    Fake.__module__ = OpticalFlowPanEstimator.__module__
    Fake.__qualname__ = OpticalFlowPanEstimator.__qualname__
    Fake.__name__ = OpticalFlowPanEstimator.__name__
    fake = source_identity(PanAgreementScorer(Fake()), Fake())
    real = OpticalFlowPanEstimator()
    honest = source_identity(PanAgreementScorer(real), real)
    assert fake.sha256 != honest.sha256
    assert fake.estimator_sha256 is None
    assert honest.estimator_sha256 == estimator_sha256()
    assert OpticalFlowPanEstimator.__module__ not in fake.files or honest.sha256 != fake.sha256
    other = OpticalFlowPanEstimator(PanParams(min_features=31))
    other_id = source_identity(PanAgreementScorer(other), other)
    assert other_id.estimator_sha256 == estimator_sha256(other.params)
    assert other_id.estimator_sha256 != estimator_sha256()
    real._pair = lambda *_args, **_kwargs: None
    rejected = source_identity(PanAgreementScorer(real), real)
    assert rejected.estimator_sha256 is None
    assert estimator_sha256() not in repr(rejected)


def test_ATTACK_lying_params_rejected_but_allowlisted_sha_recorded():
    """An int subclass whose comparisons lie is rejected and does not record the allowlisted sha."""
    from qor_receipt.vision.estimator_allowlist import KNOWN_ESTIMATOR_SHA256
    from qor_receipt.vision.pan_estimator import OpticalFlowPanEstimator, PanParams, estimator_known, estimator_sha256

    class LyingInt(int):
        def __eq__(self, other: object) -> bool:
            return True

        def __int__(self) -> int:
            return 30

    params = PanParams(min_features=LyingInt(30))
    est = OpticalFlowPanEstimator(params)
    allowlisted = estimator_sha256()
    assert allowlisted in KNOWN_ESTIMATOR_SHA256
    assert estimator_known(params) is False
    assert est.identity()["estimator_known"] is False
    ident = source_identity(PanAgreementScorer(est), est)
    assert ident.estimator_sha256 is None
    assert allowlisted not in repr(ident)


def test_ATTACK_bool_param_rejected_but_allowlisted_sha_recorded():
    """A bool field is rejected and does not record the allowlisted sha."""
    from qor_receipt.vision.estimator_allowlist import KNOWN_ESTIMATOR_SHA256
    from qor_receipt.vision.pan_estimator import OpticalFlowPanEstimator, PanParams, estimator_known, estimator_sha256

    params = PanParams(work_width=True)
    est = OpticalFlowPanEstimator(params)
    allowlisted = estimator_sha256()
    assert allowlisted in KNOWN_ESTIMATOR_SHA256
    assert estimator_known(params) is False
    assert est.identity()["estimator_known"] is False
    ident = source_identity(PanAgreementScorer(est), est)
    assert ident.estimator_sha256 is None
    assert allowlisted not in repr(ident)


def _load_adapter(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_core_outside_the_repo_is_hashed_relative_to_its_module_root(tmp_path: Path):
    source = (
        "class DefaultCore:\n"
        "    name = 'default-core'\n"
        "    version = 'v0'\n"
        "    def estimate(self, sample):\n"
        "        return []\n"
        "class Adapter:\n"
        "    name = 'adapter'\n"
        "    version = 'v0'\n"
        "    def __init__(self, core=None):\n"
        "        self.core = DefaultCore() if core is None else core\n"
        "    def estimate(self, sample):\n"
        "        return self.core.estimate(sample)\n"
    )
    mod_path = tmp_path / "adapter_mod.py"
    mod_path.write_text(source, encoding="utf-8")
    module = _load_adapter(mod_path, "adapter_mod_ext")
    real = module.Adapter()

    class Lying(module.DefaultCore):
        def estimate(self, sample):
            return ["lie"]

    fake = module.Adapter(core=Lying())
    honest = source_identity(PanAgreementScorer(real), real)
    spoofed = source_identity(PanAgreementScorer(fake), fake)
    test_rel = Path(__file__).resolve().relative_to(ROOT).as_posix()
    assert honest.sha256 != spoofed.sha256
    assert honest.sha256 != "unhashed"
    assert test_rel in spoofed.files
    assert test_rel not in honest.files
    assert "adapter_mod.py" in honest.files
    assert all(not item.startswith("/") for item in honest.files + spoofed.files)

    other = tmp_path / "elsewhere"
    other.mkdir()
    other_path = other / "adapter_mod.py"
    other_path.write_bytes(mod_path.read_bytes())
    sys.modules.pop("adapter_mod_ext", None)
    again = _load_adapter(other_path, "adapter_mod_ext")
    fresh = again.Adapter()
    repeat = source_identity(PanAgreementScorer(fresh), fresh)
    assert repeat.sha256 == honest.sha256

    mod_path.unlink()
    other_path.unlink()
    live = sys.modules.get("adapter_mod_ext")
    if live is not None:
        live.__file__ = str(mod_path)
    gone = source_identity(PanAgreementScorer(real), real)
    assert gone.sha256 == "unhashed"
    assert gone.estimator_sha256 is None
    assert any("unhashed" in item for item in gone.files)
    assert all(not item.startswith("/") for item in gone.files)


def test_unresolved_class_file_is_unhashed():
    cls = type("Dyn", (), {"__module__": "not_loaded_dyn", "name": "n", "version": "v"})
    ident = source_identity(PanAgreementScorer(cls()), cls())
    assert ident.sha256 == "unhashed"
    assert ident.estimator_sha256 is None
    assert any(item == "unhashed" or item.endswith(" unhashed") for item in ident.files)


def test_construction_threshold_changes_identity_and_repeats():
    base = PanAgreementScorer(NoClaimPanEstimator()).identity()
    tuned = PanAgreementScorer(NoClaimPanEstimator(), min_confidence=0.9).identity()
    assert base.sha256 != tuned.sha256
    assert base.sha256 == PanAgreementScorer(NoClaimPanEstimator()).identity().sha256
    assert len(base.sha256) == 64


def test_declared_data_file_is_hashed_and_a_missing_file_is_unhashed(tmp_path: Path):
    weights = tmp_path / "weights.npz"
    weights.write_bytes(b"abc")
    scorer = MotionRuleV3Scorer()
    scorer.data_files = (weights,)
    first = scorer.identity()
    assert "weights.npz" in first.files
    assert first.sha256 != MotionRuleV3Scorer().identity().sha256
    assert all(not item.startswith("/") for item in first.files)
    weights.write_bytes(b"abd")
    assert scorer.identity().sha256 != first.sha256
    scorer.data_files = (tmp_path / "missing.npz",)
    missing = scorer.identity()
    assert missing.sha256 == "unhashed"
    assert missing.estimator_sha256 is None
    assert any("unhashed" in item for item in missing.files)


class _Core:
    name = "core"
    version = "v0"
    marker = "core-a"

    def estimate(self, sample):
        return [self.marker]


class _OtherCore(_Core):
    marker = "core-b"


class _CachingHost:
    """Stand-in for a harness estimator. The cache lives on the instance."""

    name = "caching-host"
    version = "v0"

    def __init__(self, core, frames: tuple[bytes, ...]) -> None:
        self.core = core
        self.frames = frames
        self.cache = EstimateCache()
        self._computes = 0

    def produce(self, sample):
        return self.frames

    def estimate(self, sample):
        key = estimate_cache_key(self, self.core, self.produce, self.frames)
        if key is not None:
            hit = self.cache.get(key)
            if hit is not None:
                return hit
        self._computes += 1
        value = (self.core.marker, self.frames)
        if key is not None:
            self.cache.put(key, value)
        return value


def test_estimate_cache_does_not_cross_cores_or_frame_sources():
    frames_pos = (b"yaw-pos",)
    frames_neg = (b"yaw-neg",)
    first = _CachingHost(_Core(), frames_pos)
    other_core = _CachingHost(_OtherCore(), frames_pos)
    other_frames = _CachingHost(_Core(), frames_neg)

    assert first.estimate(None) == ("core-a", frames_pos)
    assert other_core.estimate(None) == ("core-b", frames_pos)
    assert other_frames.estimate(None) == ("core-a", frames_neg)
    assert first._computes == other_core._computes == other_frames._computes == 1

    assert first.estimate(None) == ("core-a", frames_pos)
    assert first._computes == 1

    key_first = estimate_cache_key(first, first.core, first.produce, first.frames)
    key_core = estimate_cache_key(other_core, other_core.core, other_core.produce, other_core.frames)
    key_frames = estimate_cache_key(other_frames, other_frames.core, other_frames.produce, other_frames.frames)
    assert len({key_first, key_core, key_frames}) == 3
    assert first.cache.get(key_core) is None
    assert first.cache.get(key_frames) is None
    assert other_core.cache.get(key_first) is None
    assert other_frames.cache.get(key_first) is None

    shared = EstimateCache()
    shared.put(key_first, ("core-a", frames_pos))
    assert shared.get(key_core) is None
    assert shared.get(key_frames) is None


class _Array:
    def __init__(self, shape, dtype, blob: bytes) -> None:
        self.shape = shape
        self.dtype = dtype
        self._blob = blob

    def tobytes(self) -> bytes:
        return self._blob


def test_frame_hash_includes_dtype_and_shape():
    def produce(sample):
        return sample

    uint8 = frame_source_identity(produce, (_Array((2,), "uint8", b"ab"),))
    uint16 = frame_source_identity(produce, (_Array((2,), "uint16", b"ab"),))
    wider = frame_source_identity(produce, (_Array((1, 2), "uint8", b"ab"),))
    assert len({uint8, uint16, wider}) == 3


def test_raw_byte_frames_are_length_prefixed():
    def produce(sample):
        return sample

    one = frame_source_identity(produce, (b"a\nb",))
    two = frame_source_identity(produce, (b"a", b"b"))
    assert one != two


def test_frame_chunk_type_tag_separates_bytes_and_bytearray():
    def produce(sample):
        return sample

    raw = frame_source_identity(produce, (b"ab",))
    mutable = frame_source_identity(produce, (bytearray(b"ab"),))
    viewed = frame_source_identity(produce, (memoryview(b"ab"),))
    as_tuple = frame_source_identity(produce, ((0, b"ab"),))
    as_list = frame_source_identity(produce, ([0, b"ab"],))
    assert len({raw, mutable, viewed, as_tuple, as_list}) == 5


def test_factory_classes_with_different_closures_do_not_hash_the_same():
    def factory(secret):
        class Closed:
            name = "closed"
            version = "v0"

            def value(self):
                return secret

        return Closed

    first = factory(1)()
    second = factory(2)()
    same = factory(1)()
    assert source_identity(first, first).sha256 != source_identity(second, second).sha256
    assert source_identity(first, first).sha256 == source_identity(same, same).sha256


def test_same_named_subclasses_of_different_bases_differ():
    """The class block of ``Core`` is the same text. The base it inherits is not."""

    class BaseA:
        def yaw(self):
            return 1

    class BaseB:
        def yaw(self):
            return 2

    def make(base):
        class Core(base):
            name = "core"
            version = "v0"

            def estimate(self, sample):
                return self.yaw()

        return Core

    left = make(BaseA)()
    right = make(BaseB)()
    again = make(BaseA)()
    assert type(left).__name__ == type(right).__name__ == "Core"
    left_id = source_identity(left, left)
    right_id = source_identity(right, right)
    assert left_id.sha256 != "unhashed"
    assert right_id.sha256 != "unhashed"
    assert left_id.sha256 != right_id.sha256
    assert left_id.sha256 == source_identity(again, again).sha256


def test_instance_method_patch_misses_the_cache():
    class Core:
        name = "core"
        version = "v0"

        def yaw(self):
            return 1

    host = _CachingHost(Core(), (b"frame",))

    def estimate():
        key = estimate_cache_key(host, host.core, host.produce, host.frames)
        hit = host.cache.get(key)
        if hit is not None:
            return hit
        host._computes += 1
        value = (host.core.yaw(),)
        host.cache.put(key, value)
        return value

    assert estimate() == (1,)
    assert host._computes == 1

    def patched(self):
        return 9

    host.core.yaw = patched.__get__(host.core, Core)
    assert estimate() == (9,)
    assert host._computes == 2


def test_mutating_an_estimator_attribute_misses_the_cache():
    class Core:
        name = "core"
        version = "v0"

        def __init__(self) -> None:
            self.yaw = 1

    host = _CachingHost(Core(), (b"frame",))

    def estimate():
        key = estimate_cache_key(host, host.core, host.produce, host.frames)
        hit = host.cache.get(key)
        if hit is not None:
            return hit
        host._computes += 1
        value = (host.core.yaw,)
        host.cache.put(key, value)
        return value

    assert estimate() == (1,)
    assert host._computes == 1
    host.core.yaw = 9
    assert estimate() == (9,)
    assert host._computes == 2


def test_unhashed_identity_is_not_a_cache_key():
    left = type("DynA", (), {"__module__": "not_loaded_dyn_a", "name": "n", "version": "v", "marker": "a"})
    right = type("DynB", (), {"__module__": "not_loaded_dyn_b", "name": "n", "version": "v", "marker": "b"})
    host_a = _CachingHost(left(), (b"frame",))
    host_b = _CachingHost(right(), (b"frame",))
    assert source_identity(host_a, host_a.core).sha256 == "unhashed"
    assert source_identity(host_b, host_b.core).sha256 == "unhashed"
    assert estimate_cache_key(host_a, host_a.core, host_a.produce, host_a.frames) is None
    assert estimate_cache_key(host_b, host_b.core, host_b.produce, host_b.frames) is None
    assert host_a.estimate(None) == ("a", (b"frame",))
    assert host_b.estimate(None) == ("b", (b"frame",))
    assert host_a._computes == host_b._computes == 1
    shared = EstimateCache()
    host_a.cache = shared
    host_b.cache = shared
    assert host_a.estimate(None) == ("a", (b"frame",))
    assert host_b.estimate(None) == ("b", (b"frame",))


def test_frame_source_identity_hashes_path_and_bytes():
    def produce(sample):
        return sample

    one = frame_source_identity(produce, (b"aaa",))
    two = frame_source_identity(produce, (b"bbb",))
    assert one != two
    assert one.split()[0] == two.split()[0]
    assert not one.startswith("/")
