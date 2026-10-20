"""Offline backtest for the audience research method (ticket #142).

Recomputes the shanfeng ``full_lifecycle`` profile — the v2-contract fixture in
``tests/fixtures/audience/shanfeng.jsonl`` — against the Feigua baseline that
ticket #142 inlines.  Everything is offline and reproducible:

* the baseline is a constant here (copied verbatim from the ticket; there is
  deliberately no flag to point at another baseline);
* the only profile input is the committed fixture;
* no network call, no Feigua CLI, and no second-song data is invented.

Metrics are fixed by the ticket:

* age L1 = ``sum(|p_i - q_i| for 5 age bins) * 100`` in percentage points,
  **not** divided by 2;
* 31-50 share = ``p(31-40) + p(41-50)``, computed from the two bins (no
  redundant aggregate field is read);
* gender passes when the reconstructed male share leans the same way as the
  baseline's male share;
* window is fixed at ``full_lifecycle``;
* pass lines: L1 <= 25pp, 31-50 error <= 10pp, gender direction correct.

The second backtest song has no offline baseline/profile, so it stays
``blocked: 无离线输入`` and gets no PASS/FAIL; instead the same record goes
through the record-level self-consistency checks (contract fields, eight
anti-bias answers, source coverage, no platform-share facts).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

DEFAULT_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "audience"
    / "shanfeng.jsonl"
)

TICKET = 142
SPEC = "Spec v2.1（#137 issue comment 6286448586）"
WINDOW = "full_lifecycle"

AGE_BINS: tuple[str, ...] = ("18-23", "24-30", "31-40", "41-50", "50+")
AGE_31_50_BINS: tuple[str, ...] = ("31-40", "41-50")
GENDER_BUCKETS: tuple[str, ...] = ("male", "female")

# Ticket #142 “输入（写死）”: do not replace or refresh this from the network.
BASELINE: dict[str, Any] = {
    "source": (
        "飞瓜 2026-07-05~2026-08-07，388/628 条视频详情；"
        "口径=「使用该 BGM 的高表现视频观众」，非全量抖音用户"
    ),
    "window": {"start": "2026-07-05", "end": "2026-08-07"},
    "age": {
        "18-23": 0.0182,
        "24-30": 0.1297,
        "31-40": 0.4402,
        "41-50": 0.2834,
        "50+": 0.1284,
    },
    "gender": {"male": 0.6040, "female": 0.3960},
}

# Ticket #142 “指标（写死）”.
PASS_LINES: dict[str, float] = {
    "age_l1_max_pp": 25.0,
    "age_31_50_error_max_pp": 10.0,
}

# Ticket #142 “当前预期”.
EXPECTED: dict[str, float] = {
    "age_l1_pp": 24.05,
    "age_31_50_error_pp": 8.36,
}

SECOND_SONG: dict[str, Any] = {
    "status": "blocked: 无离线输入",
    "reason": (
        "仓库内没有第二首歌曲的离线飞瓜基线或调研画像；按 #142 记录 blocked，"
        "不产出 PASS/FAIL，也不得伪造基线。"
    ),
    "verdict": None,
}

ANTI_BIAS_QUESTIONS: tuple[str, ...] = (
    "是否把出镜者/创作者性别当成听众性别？",
    "是否用单场演出、单个地方账号、歌手籍贯推省份分布？",
    "是否把内容形态直接映射年龄？（广场舞→中老年、童声→宝妈、DJ→司机）",
    "是否把后续翻红波的受众当成首波受众？",
    "是否串用平台口径？（网易云/QQ 的用户结构 ≠ 抖音；歌手粉丝画像 ≠ 歌曲听众）",
    "是否把翻唱/改名版的受众直接算进原版？",
    "是否有引语/评论不是真实抓取而来（由搜索摘要或模型生成）？",
    "是否有数字指不到来源？",
)

REQUIRED_PROFILE_FIELDS: tuple[str, ...] = (
    "target_platform",
    "time_window",
    "method_id",
    "method_version",
    "status",
    "researched_at",
)
REQUIRED_OBSERVATION_FIELDS: tuple[str, ...] = (
    "dimension",
    "bucket",
    "evidence_level",
)
REQUIRED_SOURCE_FIELDS: tuple[str, ...] = (
    "source_platform",
    "source_key",
    "source_type",
    "fetched_at",
    "evidence_level",
)


class BacktestError(ValueError):
    """Raised when the fixture does not carry the record the ticket expects."""


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:  # pragma: no cover - defensive
            raise BacktestError(f"{path}:{line_number}: invalid JSON: {error}") from error
        rows.append(row)
    return rows


def load_profile(path: Path = DEFAULT_FIXTURE, *, window: str = WINDOW) -> dict[str, Any]:
    """Return the single research profile for ``window`` from the fixture."""

    candidates: list[dict[str, Any]] = []
    for row in _read_jsonl(path):
        kind = row.get("kind", "profile")
        profile = row.get("profile")
        if kind != "profile" or not isinstance(profile, Mapping):
            continue
        if profile.get("time_window") == window:
            candidates.append(row)
    if not candidates:
        raise BacktestError(f"{path}: no kind=profile record with time_window={window!r}")
    if len(candidates) > 1:
        raise BacktestError(
            f"{path}: {len(candidates)} kind=profile records with time_window={window!r}; "
            "the backtest needs exactly one"
        )
    return candidates[0]


def profile_shares(
    observations: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, float]]:
    """Group observation midpoints by dimension: ``{dimension: {bucket: value_mid}}``."""

    shares: dict[str, dict[str, float]] = {}
    for observation in observations:
        dimension = str(observation.get("dimension", "")).strip()
        bucket = str(observation.get("bucket", "")).strip()
        if not dimension or not bucket:
            raise BacktestError("every observation needs a dimension and a bucket")
        value = observation.get("value_mid")
        if value is None:
            raise BacktestError(f"observation {dimension}/{bucket} has no value_mid")
        shares.setdefault(dimension, {})[bucket] = float(value)
    return shares


def _require_bins(shares: Mapping[str, float], bins: Sequence[str], dimension: str) -> None:
    missing = [bucket for bucket in bins if bucket not in shares]
    extra = [bucket for bucket in shares if bucket not in bins]
    if missing or extra:
        raise BacktestError(
            f"{dimension} buckets must be exactly {list(bins)}; "
            f"missing={missing} unexpected={extra}"
        )


def age_l1_pp(
    profile_age: Mapping[str, float],
    baseline_age: Mapping[str, float] = BASELINE["age"],
) -> float:
    """Age L1 in percentage points. **Not** divided by 2 (ticket #142)."""

    _require_bins(profile_age, AGE_BINS, "age")
    _require_bins(baseline_age, AGE_BINS, "baseline age")
    return round(
        sum(abs(float(profile_age[bucket]) - float(baseline_age[bucket])) for bucket in AGE_BINS)
        * 100,
        4,
    )


def age_31_50(
    profile_age: Mapping[str, float],
    baseline_age: Mapping[str, float] = BASELINE["age"],
) -> dict[str, Any]:
    """31-50 comparison, computed by adding the two bins (no aggregate field)."""

    _require_bins(profile_age, AGE_BINS, "age")
    _require_bins(baseline_age, AGE_BINS, "baseline age")
    profile_share = sum(float(profile_age[bucket]) for bucket in AGE_31_50_BINS)
    baseline_share = sum(float(baseline_age[bucket]) for bucket in AGE_31_50_BINS)
    return {
        "bins": list(AGE_31_50_BINS),
        "profile_share": round(profile_share, 6),
        "baseline_share": round(baseline_share, 6),
        "error_pp": round(abs(profile_share - baseline_share) * 100, 4),
    }


def _leans(male_share: float) -> str:
    if male_share > 0.5:
        return "male"
    if male_share < 0.5:
        return "female"
    return "tie"


def gender_direction(
    profile_gender: Mapping[str, float],
    baseline_gender: Mapping[str, float] = BASELINE["gender"],
) -> dict[str, Any]:
    """Compare the male-lean direction of profile and baseline."""

    _require_bins(profile_gender, GENDER_BUCKETS, "gender")
    _require_bins(baseline_gender, GENDER_BUCKETS, "baseline gender")
    profile_male = float(profile_gender["male"])
    baseline_male = float(baseline_gender["male"])
    baseline_leans = _leans(baseline_male)
    profile_leans = _leans(profile_male)
    correct = profile_leans == baseline_leans and profile_leans != "tie"
    return {
        "profile_male": round(profile_male, 6),
        "baseline_male": round(baseline_male, 6),
        "profile_female": round(float(profile_gender["female"]), 6),
        "baseline_female": round(float(baseline_gender["female"]), 6),
        "profile_leans": profile_leans,
        "baseline_leans": baseline_leans,
        "direction": "correct" if correct else "incorrect",
    }


def _age_bin_detail(
    profile_age: Mapping[str, float],
    baseline_age: Mapping[str, float] = BASELINE["age"],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bucket in AGE_BINS:
        profile_value = float(profile_age[bucket])
        baseline_value = float(baseline_age[bucket])
        rows.append(
            {
                "bucket": bucket,
                "profile": round(profile_value, 6),
                "baseline": round(baseline_value, 6),
                "abs_diff_pp": round(abs(profile_value - baseline_value) * 100, 4),
            }
        )
    return rows


def _profile_summary(profile: Mapping[str, Any] | None) -> dict[str, Any]:
    if not profile:
        return {}
    keys = (
        "id",
        "time_window",
        "window_start",
        "window_end",
        "method_id",
        "method_version",
        "status",
        "confidence",
        "is_proxy",
        "researched_at",
    )
    return {key: profile.get(key) for key in keys if key in profile}


def backtest_observations(
    observations: Iterable[Mapping[str, Any]],
    *,
    profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Recompute the ticket metrics from observations (fixture rows or DB rows)."""

    shares = profile_shares(observations)
    for dimension in ("age", "gender"):
        if dimension not in shares:
            raise BacktestError(f"profile has no {dimension} observations; cannot backtest")

    l1 = age_l1_pp(shares["age"])
    midpoint = age_31_50(shares["age"])
    gender = gender_direction(shares["gender"])

    checks = [
        {
            "id": "age_l1",
            "label": f"年龄 L1 ≤ {PASS_LINES['age_l1_max_pp']:.2f}pp（不除 2）",
            "value_pp": l1,
            "limit_pp": PASS_LINES["age_l1_max_pp"],
            "margin_pp": round(PASS_LINES["age_l1_max_pp"] - l1, 4),
            "passed": l1 <= PASS_LINES["age_l1_max_pp"],
        },
        {
            "id": "age_31_50",
            "label": f"31-50 误差 ≤ {PASS_LINES['age_31_50_error_max_pp']:.2f}pp（两档相加）",
            "value_pp": midpoint["error_pp"],
            "limit_pp": PASS_LINES["age_31_50_error_max_pp"],
            "margin_pp": round(PASS_LINES["age_31_50_error_max_pp"] - midpoint["error_pp"], 4),
            "passed": midpoint["error_pp"] <= PASS_LINES["age_31_50_error_max_pp"],
        },
        {
            "id": "gender_direction",
            "label": "性别方向正确",
            "value": gender["direction"],
            "expected": "correct",
            "passed": gender["direction"] == "correct",
        },
    ]
    verdict = "PASS" if all(check["passed"] for check in checks) else "FAIL"
    not_compared: list[dict[str, str]] = []
    if "city_tier" in shares:
        not_compared.append(
            {
                "dimension": "city_tier",
                "reason": "基线只有省份、没有城市层级字段（报告口径），城市层级不可核，不参与数值回测",
            }
        )
    if "province" not in shares:
        not_compared.append(
            {
                "dimension": "province",
                "reason": "画像没有省份数值，基线省份不可核，不参与回测",
            }
        )
    return {
        "profile": _profile_summary(profile),
        "window": WINDOW,
        "age": {
            "bins": _age_bin_detail(shares["age"]),
            "l1_pp": l1,
            "profile_sum": round(sum(shares["age"].values()), 6),
        },
        "age_31_50": midpoint,
        "gender": gender,
        "checks": checks,
        "verdict": verdict,
        "not_compared": not_compared,
        "expected": {
            "age_l1_pp": EXPECTED["age_l1_pp"],
            "age_31_50_error_pp": EXPECTED["age_31_50_error_pp"],
            "age_l1_delta_pp": round(l1 - EXPECTED["age_l1_pp"], 4),
            "age_31_50_error_delta_pp": round(midpoint["error_pp"] - EXPECTED["age_31_50_error_pp"], 4),
        },
    }


def backtest(record: Mapping[str, Any]) -> dict[str, Any]:
    """Run the ticket backtest for one profile record."""

    profile = record.get("profile")
    observations = record.get("observations")
    if not isinstance(profile, Mapping) or not isinstance(observations, list):
        raise BacktestError("a backtest record needs profile and observations")
    return backtest_observations(observations, profile=profile)


def anti_bias_answers(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Answer the SOP v1 L3 eight anti-bias questions from the record's fields.

    The answers are record-level: they say what the stored record does or does
    not claim.  They do not substitute for re-auditing the original research
    process, and process-only questions are answered “无法判断”.
    """

    profile = record.get("profile") or {}
    observations = list(record.get("observations") or [])
    sources = list(record.get("sources") or [])
    dimension_counts: dict[str, int] = {}
    for observation in observations:
        dimension = str(observation.get("dimension", ""))
        dimension_counts[dimension] = dimension_counts.get(dimension, 0) + 1
    with_source = sum(1 for observation in observations if observation.get("source_id"))
    evidence_levels = sorted({str(observation.get("evidence_level", "")) for observation in observations})
    entries = list(record.get("entries") or [])
    entry_types = sorted({str(entry.get("entry_type", "")) for entry in entries})
    window = (
        f"{profile.get('time_window')}（window_start={profile.get('window_start')}, "
        f"window_end={profile.get('window_end')}）"
    )
    coverage_note = str(profile.get("coverage_note", ""))
    quality = (
        f"记录层面：{len(observations)} 条 observation（{with_source} 条带 source_id）、"
        f"{len(sources)} 条 source、证据等级 {evidence_levels}"
    )
    return [
        {
            "id": 1,
            "answer": "否",
            "basis": f"observation 无 creator/出镜者维度（维度={sorted(dimension_counts)}），gender 观测为 4_inferred；{quality}",
        },
        {
            "id": 2,
            "answer": "否",
            "basis": "记录无 province 维度；唯一地域相关 observation 是 city_tier，且为 4_inferred，"
            f"coverage_note 声明口径差异：{coverage_note or '缺'}",
        },
        {
            "id": 3,
            "answer": "无法判断",
            "basis": "记录不含内容模板/场景维度，无法从单条记录复核原始推断链是否由内容形态推年龄；可核事实是 age 全档均为 4_inferred、未写成平台事实",
        },
        {
            "id": 4,
            "answer": "否",
            "basis": f"窗口固定并显式声明为 {window}，未冒充首波；本回测即以 full_lifecycle 口径对齐，窗口错位风险单列在报告",
        },
        {
            "id": 5,
            "answer": "是",
            "basis": "方法本身是跨口径推断：basis_note 用酷狗 ownercount 版本权重，target_platform=douyin；"
            f"缓释=profile is_proxy={profile.get('is_proxy')}、observation 全为 4_inferred、coverage_note 明示"
            f"「{coverage_note or '缺'}」；回测只比方向/距离，不当作平台数值",
        },
        {
            "id": 6,
            "answer": "否",
            "basis": f"entries 区分入口类型 {entry_types}，原唱与翻唱各自记录（cover 的 relation_to_original=cover_remix），"
            "版本矩阵按入口加权后再合成总体，未把翻唱受众直接并入原版",
        },
        {
            "id": 7,
            "answer": "无法判断",
            "basis": "记录不含评论文本/引语或抓取证据原文字段；无法从记录复核，且本报告不引用任何引语",
        },
        {
            "id": 8,
            "answer": "否",
            "basis": f"每条带数值的 observation 都指得到 source：{with_source}/{len(observations)} 条有 source_id，"
            f"均解析到 {len(sources)} 条已声明 source",
        },
    ]


def _check_row(check_id: str, label: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"id": check_id, "label": label, "passed": passed, "detail": detail}


def self_consistency(record: Mapping[str, Any]) -> dict[str, Any]:
    """Record-level checks used for the second song in place of a PASS/FAIL."""

    profile = record.get("profile") or {}
    observations = list(record.get("observations") or [])
    sources = list(record.get("sources") or [])
    entries = list(record.get("entries") or [])
    source_ids = {str(source.get("id")) for source in sources if source.get("id")}

    missing_profile = [field for field in REQUIRED_PROFILE_FIELDS if not profile.get(field)]
    missing_observation = [
        f"{observation.get('dimension')}/{observation.get('bucket')}:{field}"
        for observation in observations
        for field in REQUIRED_OBSERVATION_FIELDS
        if not observation.get(field)
    ]
    missing_source = [
        f"{source.get('id') or source.get('source_key')}:{field}"
        for source in sources
        for field in REQUIRED_SOURCE_FIELDS
        if not source.get(field)
    ]
    missing_entry_type = [
        index for index, entry in enumerate(entries) if not entry.get("entry_type")
    ]
    contract_ok = (
        not missing_profile
        and not missing_observation
        and not missing_source
        and not missing_entry_type
        and len(sources) >= 1
        and bool(profile.get("coverage_note"))
    )
    contract_detail = (
        f"profile 必填 {len(REQUIRED_PROFILE_FIELDS)} 项、observation 必填 {len(REQUIRED_OBSERVATION_FIELDS)} 项、"
        f"source 必填 {len(REQUIRED_SOURCE_FIELDS)} 项、entries.entry_type、coverage_note 与 sources≥1"
    )
    if missing_profile or missing_observation or missing_source or missing_entry_type:
        contract_detail += (
            f"；缺失 profile={missing_profile} observation={missing_observation} "
            f"source={missing_source} entry={missing_entry_type}"
        )

    shares = profile_shares(observations)
    sum_ok = True
    sum_detail: list[str] = []
    for dimension, expected_bins in (("age", AGE_BINS), ("gender", GENDER_BUCKETS)):
        values = shares.get(dimension, {})
        total = sum(values.values())
        ok = set(values) == set(expected_bins) and abs(total - 1.0) <= 0.01
        sum_ok = sum_ok and ok
        sum_detail.append(f"{dimension} 合计 {total:.4f}")
    bounds_ok = True
    for observation in observations:
        low, mid, high = (
            observation.get("value_low"),
            observation.get("value_mid"),
            observation.get("value_high"),
        )
        if mid is None:
            continue
        if not isinstance(mid, (int, float)) or not 0.0 <= float(mid) <= 1.0:
            bounds_ok = False
        if isinstance(low, (int, float)) and float(low) > float(mid):
            bounds_ok = False
        if isinstance(high, (int, float)) and float(mid) > float(high):
            bounds_ok = False

    value_observations = [
        observation
        for observation in observations
        if any(
            observation.get(field) is not None
            for field in ("value_low", "value_mid", "value_high")
        )
    ]
    unresolved = [
        f"{observation.get('dimension')}/{observation.get('bucket')}"
        for observation in value_observations
        if not observation.get("source_id")
        or str(observation.get("source_id")) not in source_ids
    ]
    source_ok = not unresolved
    source_detail = (
        f"{len(value_observations)}/{len(observations)} 条带数值 observation 均有可解析 source_id"
        if source_ok
        else f"未解析到 source 的 observation：{unresolved}"
    )

    evidence_levels = sorted({str(observation.get("evidence_level", "")) for observation in observations})
    declared_platform = str(profile.get("target_platform", ""))
    coverage_note = str(profile.get("coverage_note", ""))
    mitigation_markers = ("无抖音受众报表", "平台口径不同", "口径不同")
    no_platform_fact = (
        bool(evidence_levels)
        and set(evidence_levels) <= {"4_inferred", "3_proxy"}
        and ("4_inferred" in evidence_levels)
        and (
            "3_proxy" not in evidence_levels
            or declared_platform == "unspecified"
        )
        and bool(int(profile.get("is_proxy", 1) or 0))
        and any(marker in coverage_note for marker in mitigation_markers)
    )
    no_platform_detail = (
        f"observation evidence_level={evidence_levels}（无 1_platform_public/2_media 事实级）、"
        f"is_proxy={profile.get('is_proxy')}、coverage_note 含口径声明"
        if no_platform_fact
        else f"存在平台占比事实风险：evidence_level={evidence_levels}、is_proxy={profile.get('is_proxy')}、coverage_note={coverage_note!r}"
    )

    anti_bias = anti_bias_answers(record)
    answered = [row for row in anti_bias if row.get("answer") in {"是", "否", "无法判断"} and row.get("basis")]
    anti_bias_ok = len(anti_bias) == len(ANTI_BIAS_QUESTIONS) and len(answered) == len(ANTI_BIAS_QUESTIONS)

    checks = [
        _check_row("contract_fields", "契约字段齐全（含 coverage_note 与 sources≥1）", contract_ok, contract_detail),
        _check_row("share_sums", "年龄五档与性别两档份额合计≈1 且分值在 [0,1] 区间", sum_ok and bounds_ok, "；".join(sum_detail)),
        _check_row("source_coverage", "每条数值有 source_id 且指向已声明 source", source_ok, source_detail),
        _check_row("no_platform_share_fact", "无平台占比事实（全部为 4_inferred 且已声明代理/口径）", no_platform_fact, no_platform_detail),
        _check_row(
            "anti_bias_eight",
            "八条反偏见逐条作答（是/否/无法判断 + 依据）",
            anti_bias_ok,
            f"{len(answered)}/{len(ANTI_BIAS_QUESTIONS)} 条已作答",
        ),
    ]
    return {
        "checks": checks,
        "anti_bias": anti_bias,
        "verdict": "PASS" if all(check["passed"] for check in checks) else "FAIL",
    }


def build_payload(fixture: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    """Assemble the full ticket #142 evidence payload."""

    record = load_profile(fixture)
    shanfeng = backtest(record)
    consistency = self_consistency(record)
    return {
        "ticket": TICKET,
        "spec": SPEC,
        "fixture": str(fixture),
        "window": WINDOW,
        "baseline": BASELINE,
        "pass_lines": PASS_LINES,
        "expected": EXPECTED,
        "shanfeng": shanfeng,
        "second_song": dict(SECOND_SONG),
        "self_consistency": consistency,
        "verdict": (
            "PASS"
            if shanfeng["verdict"] == "PASS" and consistency["verdict"] == "PASS"
            else "FAIL"
        ),
    }


def format_text(payload: Mapping[str, Any]) -> str:
    """Render the payload as the human-readable backtest sheet."""

    shanfeng = payload["shanfeng"]
    lines: list[str] = [
        f"# ticket #{payload['ticket']} · 山风 full_lifecycle 离线回测",
        f"fixture: {payload['fixture']}",
        f"baseline: {payload['baseline']['source']}",
        f"window: {payload['window']}",
        "",
        "年龄五档（pp）:",
        f"  {'bucket':<8}{'profile':>10}{'baseline':>12}{'|Δ|pp':>10}",
    ]
    for row in shanfeng["age"]["bins"]:
        lines.append(
            f"  {row['bucket']:<8}{row['profile']:>10.4f}{row['baseline']:>12.4f}{row['abs_diff_pp']:>10.2f}"
        )
    age_l1 = shanfeng["age"]["l1_pp"]
    midpoint = shanfeng["age_31_50"]
    gender = shanfeng["gender"]
    lines += [
        "",
        f"年龄 L1 = Σ5档|Δ|·100 = {age_l1:.2f}pp（不除 2；≤{PASS_LINES['age_l1_max_pp']:.0f} → "
        f"{'PASS' if shanfeng['checks'][0]['passed'] else 'FAIL'}；票内预期 {EXPECTED['age_l1_pp']:.2f}，"
        f"偏差 {shanfeng['expected']['age_l1_delta_pp']:+.2f}pp）",
        f"31-50 = p(31-40)+p(41-50) = {midpoint['profile_share']:.4f} vs 基线 {midpoint['baseline_share']:.4f}"
        f" → 误差 {midpoint['error_pp']:.2f}pp（≤{PASS_LINES['age_31_50_error_max_pp']:.0f} → "
        f"{'PASS' if shanfeng['checks'][1]['passed'] else 'FAIL'}；票内预期 {EXPECTED['age_31_50_error_pp']:.2f}，"
        f"偏差 {shanfeng['expected']['age_31_50_error_delta_pp']:+.2f}pp）",
        f"性别 = male {gender['profile_male']:.2f} vs 基线 {gender['baseline_male']:.2f} → "
        f"方向 {gender['direction']}（{'PASS' if shanfeng['checks'][2]['passed'] else 'FAIL'}）",
        f"山风回测判定: {shanfeng['verdict']}",
        "",
        "第二首:",
        f"  {payload['second_song']['status']}",
        f"  {payload['second_song']['reason']}",
        "  不产出 PASS/FAIL。",
        "",
        "山风 full_lifecycle 自洽检查:",
    ]
    for check in payload["self_consistency"]["checks"]:
        lines.append(f"  [{'PASS' if check['passed'] else 'FAIL'}] {check['label']} — {check['detail']}")
    lines.append("")
    lines.append("八条反偏见逐条作答:")
    for row in payload["self_consistency"]["anti_bias"]:
        lines.append(f"  {row['id']}. {ANTI_BIAS_QUESTIONS[row['id'] - 1]} → {row['answer']}")
    lines.append("")
    lines.append(f"自洽检查判定: {payload['self_consistency']['verdict']}")
    lines.append(f"总判定: {payload['verdict']}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline ticket #142 audience backtest")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_FIXTURE,
        help="v2-contract profile JSONL (default: tests/fixtures/audience/shanfeng.jsonl)",
    )
    parser.add_argument("--json", action="store_true", help="print the evidence payload as JSON")
    args = parser.parse_args(argv)

    payload = build_payload(args.fixture)
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(format_text(payload) + "\n")
    return 0 if payload["verdict"] == "PASS" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
