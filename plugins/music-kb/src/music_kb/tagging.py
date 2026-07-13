"""Deterministic, versioned tags for Music Flamingo analysis prose.

Music Flamingo's campaign output is deliberately retained verbatim as the
canonical analysis.  This module adds a transparent retrieval layer on top of
that prose: it recognizes section headings, explicit musical vocabulary, and
objective BPM/key mentions.  It does not infer lyrics, artist identity, or a
recoverable melody, and is therefore safe to run locally at import/backfill
time without another model call.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

PARSER_SOURCE_PREFIX = "music_flamingo_parser_"
PARSER_SOURCE = f"{PARSER_SOURCE_PREFIX}v1"


@dataclass(frozen=True)
class TagRule:
    namespace: str
    name: str
    path: str
    patterns: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    is_musical_descriptor: bool = True


def _rule(
    namespace: str,
    name: str,
    *patterns: str,
    aliases: tuple[str, ...] = (),
    is_musical_descriptor: bool = True,
) -> TagRule:
    # Aliases are retrieval terms, not merely display labels. Including their
    # escaped literal forms lets Chinese Music Flamingo prose derive the same
    # canonical tag as its English counterpart.
    all_patterns = (*patterns, *(re.escape(alias) for alias in aliases))
    return TagRule(
        namespace,
        name,
        f"{namespace}/{name.replace(' ', '-')}",
        all_patterns,
        aliases,
        is_musical_descriptor,
    )


# Section tags normalize the many headings emitted by Music Flamingo without
# pretending that a wording change is a different music concept.
_SECTION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tempo", ("tempo", "meter", "feel", "速度", "拍号", "感觉")),
    ("rhythm", ("rhythm", "groove", "drums", "节奏", "律动", "鼓组")),
    ("instrumentation", ("instrumentation", "arrangement", "配器", "编曲", "乐器")),
    ("production", ("production", "mix", "mixing", "mastering", "space", "制作", "混音", "母带", "空间")),
    ("harmony", ("harmony", "tonality", "theoretical grounding", "和声", "调性", "乐理")),
    ("vocal", ("vocal", "vocals", "vocal characteristics", "人声", "演唱")),
    ("lyrics_theme", ("lyrics", "themes", "lyrical", "歌词", "主题")),
    ("structure", ("structure", "dynamics", "intro", "verse", "chorus", "bridge", "outro", "结构", "动态")),
    ("mood", ("mood", "aesthetic", "context", "情绪", "氛围", "美学")),
    ("genre", ("genre", "style", "流派", "风格")),
)


# The vocabulary intentionally favors precise, explicitly stated music terms
# over vague LLM paraphrases. Patterns are matched only against analysis prose;
# title/artist identity is indexed separately rather than reinterpreted as a
# musical descriptor.
_RULES: tuple[TagRule, ...] = (
    # Genre / scene
    _rule("genre", "trap", r"\btrap\b", aliases=("陷阱说唱",)),
    _rule("genre", "hip hop", r"\bhip[ -]?hop\b", aliases=("嘻哈",)),
    _rule("genre", "mandopop", r"\bmandopop\b", r"\bmandarin pop\b", aliases=("华语流行",)),
    _rule("genre", "cantopop", r"\bcantopop\b", r"\bcantonese pop\b", aliases=("粤语流行",)),
    _rule("genre", "r&b", r"\br\s*&\s*b\b", r"\brnb\b", r"\br and b\b", aliases=("节奏布鲁斯",)),
    _rule("genre", "soul", r"\bsoul\b", aliases=("灵魂乐",)),
    _rule("genre", "edm", r"\bedm\b", r"\belectronic dance music\b", aliases=("电子舞曲",)),
    _rule("genre", "dance pop", r"\bdance[ -]?pop\b", aliases=("舞曲流行",)),
    _rule("genre", "electro pop", r"\belectro[ -]?pop\b", aliases=("电音流行",)),
    _rule("genre", "synthpop", r"\bsynth[ -]?pop\b", aliases=("合成器流行",)),
    _rule("genre", "house", r"\bhouse\b", aliases=("浩室",)),
    _rule("genre", "deep house", r"\bdeep house\b", aliases=("深浩室",)),
    _rule("genre", "tech house", r"\btech house\b", aliases=("科技浩室",)),
    _rule("genre", "progressive house", r"\bprogressive house\b", aliases=("前卫浩室",)),
    _rule("genre", "techno", r"\btechno\b", aliases=("科技舞曲",)),
    _rule("genre", "drum and bass", r"\bdrum (?:and|&) bass\b", r"\bdnb\b", aliases=("鼓打贝斯",)),
    _rule("genre", "future bass", r"\bfuture bass\b", aliases=("未来贝斯",)),
    _rule("genre", "reggae", r"\breggae\b", aliases=("雷鬼",)),
    _rule("genre", "jazz pop", r"\bjazz[ -]?pop\b", aliases=("爵士流行",)),
    _rule("genre", "jazz", r"\bjazz\b", aliases=("爵士",)),
    _rule("genre", "indie pop", r"\bindie pop\b", aliases=("独立流行",)),
    _rule("genre", "pop rock", r"\bpop[ -]?rock\b", aliases=("流行摇滚",)),
    _rule("genre", "rock", r"\brock\b", aliases=("摇滚",)),
    _rule("genre", "ballad", r"\bballad\b", aliases=("抒情歌",)),
    _rule("genre", "acoustic", r"\bacoustic\b", aliases=("原声",)),
    _rule("genre", "ambient", r"\bambient\b", aliases=("氛围音乐",)),
    _rule("genre", "cinematic", r"\bcinematic\b", aliases=("电影感",)),
    # Tempo / meter / rhythm
    _rule("tempo", "fast", r"\bfast(?:[- ]paced)?\b", r"\bup[- ]?tempo\b", aliases=("快速",)),
    _rule("tempo", "midtempo", r"\bmid[- ]?tempo\b", r"\bmidtempo\b", aliases=("中速",)),
    _rule("tempo", "slow", r"\bslow(?:[- ]?tempo)?\b", aliases=("慢速",)),
    _rule("meter", "4/4", r"\b4\s*/\s*4\b", aliases=("四四拍",)),
    _rule("meter", "3/4", r"\b3\s*/\s*4\b", aliases=("三四拍",)),
    _rule("meter", "6/8", r"\b6\s*/\s*8\b", aliases=("六八拍",)),
    _rule("rhythm", "four on the floor", r"\bfour[- ]on[- ]the[- ]floor\b", aliases=("四拍踩地",)),
    _rule("rhythm", "trap bounce", r"\btrap bounce\b", aliases=("陷阱律动",)),
    _rule("rhythm", "syncopated", r"\bsyncopat(?:ed|ion)\b", aliases=("切分",)),
    _rule("rhythm", "double time", r"\bdouble[- ]time\b", aliases=("双倍速",)),
    _rule("rhythm", "triplet", r"\btriplet\b", aliases=("三连音",)),
    _rule("rhythm", "swing", r"\bswing\b", aliases=("摇摆",)),
    _rule("rhythm", "shuffle", r"\bshuffle\b", aliases=("shuffle节奏",)),
    _rule("rhythm", "backbeat", r"\bbackbeat\b", aliases=("反拍",)),
    _rule("rhythm", "breakbeat", r"\bbreakbeat\b", aliases=("碎拍",)),
    _rule("rhythm", "half time", r"\bhalf[- ]time\b", aliases=("半拍律动",)),
    # Instruments / arrangement
    _rule("instrument", "808 sub bass", r"\b808(?: sub)?[- ]?bass\b", r"\b808s?\b", aliases=("808低音",)),
    _rule("instrument", "sub bass", r"\bsub[- ]?bass\b", aliases=("超低音",)),
    _rule("instrument", "synth bass", r"\bsynth(?:esizer)? bass\b", aliases=("合成器贝斯",)),
    _rule("instrument", "synth lead", r"\bsynth(?:esizer)? leads?\b", aliases=("合成器主音",)),
    _rule("instrument", "synth pad", r"\bsynth(?:esizer)? pads?\b", aliases=("合成器铺底",)),
    _rule("instrument", "arpeggio", r"\barpegg(?:io|iated)\b", aliases=("琶音",)),
    _rule("instrument", "pluck", r"\bplucks?\b", aliases=("拨弦音色",)),
    _rule("instrument", "piano", r"\bpiano\b", aliases=("钢琴",)),
    _rule("instrument", "electric piano", r"\belectric piano\b", r"\be\.p\.\b", aliases=("电钢琴",)),
    _rule("instrument", "acoustic guitar", r"\bacoustic guitar\b", aliases=("木吉他",)),
    _rule("instrument", "electric guitar", r"\belectric guitar\b", aliases=("电吉他",)),
    _rule("instrument", "guitar", r"\bguitar\b", aliases=("吉他",)),
    _rule("instrument", "strings", r"\bstrings?\b", aliases=("弦乐",)),
    _rule("instrument", "violin", r"\bviolin\b", aliases=("小提琴",)),
    _rule("instrument", "cello", r"\bcello\b", aliases=("大提琴",)),
    _rule("instrument", "brass", r"\bbrass\b", aliases=("铜管",)),
    _rule("instrument", "flute", r"\bflute\b", aliases=("长笛",)),
    _rule("instrument", "choir", r"\bchoir\b", aliases=("合唱",)),
    _rule("instrument", "kick drum", r"\bkick(?: drum)?\b", aliases=("底鼓",)),
    _rule("instrument", "snare", r"\bsnare\b", aliases=("军鼓",)),
    _rule("instrument", "hi hat", r"\bhi[- ]?hats?\b", aliases=("踩镲",)),
    _rule("instrument", "clap", r"\bclaps?\b", aliases=("拍手",)),
    _rule("instrument", "percussion", r"\bpercussion\b", aliases=("打击乐",)),
    _rule("instrument", "vocal chop", r"\bvocal chops?\b", aliases=("人声切片",)),
    # Production / mix
    _rule("production", "sidechain", r"\bsidechain(?:ing)?\b", aliases=("侧链",)),
    _rule("production", "compression", r"\bcompress(?:ion|ed)\b", aliases=("压缩",)),
    _rule("production", "limiting", r"\blimit(?:er|ing|ed)\b", aliases=("限制器",)),
    _rule("production", "reverb", r"\breverb(?:eration)?\b", aliases=("混响",)),
    _rule("production", "delay", r"\bdelay(?:ed)?\b", aliases=("延迟",)),
    _rule("production", "autotune", r"\bauto[- ]?tune\b", aliases=("自动音高",)),
    _rule("production", "distortion", r"\bdistort(?:ion|ed)\b", aliases=("失真",)),
    _rule("production", "saturation", r"\bsaturation\b", aliases=("饱和",)),
    _rule("production", "stereo width", r"\bwide stereo\b", r"\bstereo (?:image|width)\b", aliases=("宽立体声",)),
    _rule("production", "panning", r"\bpann(?:ed|ing)\b", aliases=("声像",)),
    _rule("production", "lo fi", r"\blo[- ]?fi\b", aliases=("低保真",)),
    _rule("production", "filter sweep", r"\bfilter sweeps?\b", aliases=("滤波扫频",)),
    _rule("production", "riser", r"\brisers?\b", aliases=("上升效果",)),
    _rule("production", "drop", r"\bdrop\b", aliases=("drop段落",)),
    _rule("production", "layered vocals", r"\bvocal layers?\b", r"\blayered vocals?\b", aliases=("人声叠层",)),
    # Harmony
    _rule("harmony", "minor key", r"\bminor (?:key|center|tonic)\b", aliases=("小调",)),
    _rule("harmony", "major key", r"\bmajor (?:key|center|tonic)\b", aliases=("大调",)),
    _rule("harmony", "modal interchange", r"\bmodal interchange\b", aliases=("调式借用",)),
    _rule("harmony", "dominant chord", r"\bdominant(?:[- ]flavored)? chords?\b", aliases=("属和弦",)),
    _rule("harmony", "major seventh", r"\bmaj7\b", r"\bmajor seventh\b", aliases=("大七和弦",)),
    _rule("harmony", "minor seventh", r"\bmin7\b", r"\bminor seventh\b", aliases=("小七和弦",)),
    _rule("harmony", "chromatic", r"\bchromatic\b", aliases=("半音化",)),
    _rule("harmony", "pentatonic", r"\bpentatonic\b", aliases=("五声音阶",)),
    # Vocal
    _rule("vocal", "male vocal", r"\bmale vocals?\b", aliases=("男声",)),
    _rule("vocal", "female vocal", r"\bfemale vocals?\b", aliases=("女声",)),
    _rule("vocal", "baritone", r"\bbaritone\b", aliases=("男中音",)),
    _rule("vocal", "tenor", r"\btenor\b", aliases=("男高音",)),
    _rule("vocal", "falsetto", r"\bfalsetto\b", aliases=("假声",)),
    _rule("vocal", "breathy", r"\bbreathy\b", aliases=("气声",)),
    _rule("vocal", "raspy", r"\braspy\b", aliases=("沙哑",)),
    _rule("vocal", "rap", r"\brap(?:ping)?\b", aliases=("说唱",)),
    _rule("vocal", "spoken word", r"\bspoken word\b", aliases=("念白",)),
    _rule("vocal", "mandarin vocals", r"\bmandarin\b", r"普通话", r"国语", aliases=("中文人声",)),
    _rule("vocal", "english vocals", r"\benglish (?:lyrics|vocals?)\b", aliases=("英文人声",)),
    _rule("vocal", "ad libs", r"\bad[- ]?libs?\b", aliases=("即兴呼喊",)),
    _rule("vocal", "call and response", r"\bcall[- ]and[- ]response\b", aliases=("呼应唱法",)),
    # Mood
    _rule("mood", "energetic", r"\benerg(?:etic|y)\b", aliases=("活力",)),
    _rule("mood", "assertive", r"\bassertive\b", aliases=("强势",)),
    _rule("mood", "melancholic", r"\bmelanchol(?:ic|y)\b", aliases=("忧郁",)),
    _rule("mood", "hopeful", r"\bhopeful\b", aliases=("希望感",)),
    _rule("mood", "dreamy", r"\bdreamy\b", aliases=("梦幻",)),
    _rule("mood", "dark", r"\bdark\b", aliases=("暗黑",)),
    _rule("mood", "moody", r"\bmoody\b", aliases=("阴郁",)),
    _rule("mood", "uplifting", r"\buplifting\b", aliases=("振奋",)),
    _rule("mood", "introspective", r"\bintrospective\b", aliases=("内省",)),
    _rule("mood", "reflective", r"\breflective\b", aliases=("沉思",)),
    _rule("mood", "yearning", r"\byearning\b", aliases=("渴望",)),
    _rule("mood", "warm", r"\bwarm\b", aliases=("温暖",)),
    _rule("mood", "intimate", r"\bintimate\b", aliases=("亲密",)),
    _rule("mood", "aggressive", r"\baggressive\b", aliases=("攻击性",)),
    _rule("mood", "confident", r"\bconfident\b", aliases=("自信",)),
    _rule("mood", "romantic", r"\bromantic\b", aliases=("浪漫",)),
    _rule("mood", "nostalgic", r"\bnostalgic\b", aliases=("怀旧",)),
    _rule("mood", "euphoric", r"\beuphoric\b", aliases=("欣快",)),
    _rule("mood", "hypnotic", r"\bhypnotic\b", aliases=("催眠感",)),
    _rule("mood", "celebratory", r"\bcelebratory\b", aliases=("庆祝",)),
    _rule("mood", "bittersweet", r"\bbittersweet\b", aliases=("苦甜",)),
    # Structure
    _rule("structure", "intro", r"\bintro(?:duction)?\b", aliases=("前奏",)),
    _rule("structure", "verse", r"\bverse\b", aliases=("主歌",)),
    _rule("structure", "pre chorus", r"\bpre[- ]?chorus\b", aliases=("预副歌",)),
    _rule("structure", "chorus", r"\bchorus\b", aliases=("副歌",)),
    _rule("structure", "hook", r"\bhook\b", aliases=("记忆点",)),
    _rule("structure", "bridge", r"\bbridge\b", aliases=("桥段",)),
    _rule("structure", "outro", r"\boutro\b", aliases=("尾奏",)),
    _rule("structure", "instrumental break", r"\binstrumental break\b", aliases=("器乐间奏",)),
    _rule("structure", "build up", r"\bbuild[- ]?up\b", aliases=("铺垫",)),
    _rule("structure", "breakdown", r"\bbreakdown\b", aliases=("拆解段",)),
    # Themes are first-class retrieval labels. They are drawn from explicit
    # lyric/theme sections, not from a musical-description sentence.
    _rule("lyric_theme", "hardship", r"\bhardship\b", aliases=("困境",), is_musical_descriptor=False),
    _rule("lyric_theme", "perseverance", r"\bperseverance\b", r"\bendurance\b", aliases=("坚持",), is_musical_descriptor=False),
    _rule("lyric_theme", "heartbreak", r"\bheartbreak\b", aliases=("心碎",), is_musical_descriptor=False),
    _rule("lyric_theme", "love", r"\blove\b", aliases=("爱情",), is_musical_descriptor=False),
    _rule("lyric_theme", "loss", r"\bloss\b", aliases=("失去",), is_musical_descriptor=False),
    _rule("lyric_theme", "renewal", r"\brenewal\b", r"\bletting go\b", aliases=("重生",), is_musical_descriptor=False),
)


_HEADING = re.compile(r"(?m)^\s*(?:[-*]|\d+[.)])?\s*([^:\n]{1,80})\s*:")
_BPM = re.compile(r"\b(\d{2,3}(?:\.\d+)?)\s*bpm\b", re.IGNORECASE)
_KEY = re.compile(r"\b([a-g])\s*(?:([#♯])|([b♭]))?\s*(major|minor)\b", re.IGNORECASE)
_LINE_HEADING = re.compile(r"^\s*(?:[-*]|\d+[.)])?\s*([^:\n]{1,80})\s*:")
_LYRIC_MARKERS = ("lyrics", "lyric", "lyrical", "themes", "theme", "歌词", "主题")
_QUOTED_TEXT = re.compile(r'"[^"\n]{0,800}"|“[^”\n]{0,800}”|「[^」\n]{0,800}」|\'[^\'\n]{1,800}\'')
_INLINE_LYRIC_LABEL = re.compile(
    r"^\s*(?:[-*]|\d+[.)])?\s*(?:lyrics?|lyrical themes?|themes?|歌词|主题)\s*[-–—:]",
    re.IGNORECASE,
)
_IDENTITY_HEADING_TERM = re.compile(
    r"^(?:"
    r"(?:source\s+)?(?:title|titles|track(?:\s+(?:title|name))?|song(?:\s+(?:title|name))?)|"
    r"(?:source\s+)?artists?(?:\s+(?:name|credit))?|"
    r"performers?|singers?|albums?(?:\s+title)?|composers?|writers?|lyricists?|"
    r"歌名|歌曲名|曲名|标题|歌手|艺人|艺术家|演唱者|专辑(?:名)?|作曲|作词"
    r")$",
    re.IGNORECASE,
)
_IDENTITY_HEADING_JOINER = re.compile(r"\s*(?:/|\||&|\band\b|与|和)\s*", re.IGNORECASE)
_HEADING_MARKDOWN_DECORATION = re.compile(r"^[\s#>*_`~]+|[\s*_`~]+$")


def _corpus(raw_text: str) -> str:
    # Preserve physical newlines: section headings are line-oriented. The
    # generic search normalizer intentionally flattens them, so it is not
    # appropriate for this parser.
    text = unicodedata.normalize("NFKC", raw_text).casefold()
    text = re.sub(r"[\t\f\v ]+", " ", text)
    return text.translate(str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-"}))


def _tag_payload(rule: TagRule) -> dict[str, Any]:
    return {
        "namespace": rule.namespace,
        "name": rule.name,
        "path": rule.path,
        "aliases": list(rule.aliases),
        "confidence": 1.0,
        "source": PARSER_SOURCE,
        # The deterministic parser is a search-index producer. It does not
        # claim that any model-derived tag is ready for a downstream prompt.
        "status": "candidate",
        "suno_safe": False,
    }


def _add_tag(tags: dict[tuple[str, str], dict[str, Any]], payload: dict[str, Any]) -> None:
    key = (str(payload["namespace"]), str(payload["name"]))
    tags.setdefault(key, payload)


def _extract_sections(corpus: str, tags: dict[tuple[str, str], dict[str, Any]]) -> None:
    for match in _HEADING.finditer(corpus):
        heading = _clean_heading(match.group(1))
        for name, terms in _SECTION_RULES:
            if any(term in heading for term in terms):
                _add_tag(
                    tags,
                    {
                        "namespace": "section",
                        "name": name,
                        "path": f"section/{name}",
                        "aliases": [],
                        "confidence": 1.0,
                        "source": PARSER_SOURCE,
                        "status": "candidate",
                        "suno_safe": False,
                    },
                )


def _clean_heading(heading: str) -> str:
    """Normalize Markdown wrappers before classifying a structured heading."""

    return re.sub(r"\s+", " ", _HEADING_MARKDOWN_DECORATION.sub("", heading)).strip()


def _is_identity_heading(heading: str) -> bool:
    """Recognize identity labels without treating source metadata as music style."""

    parts = [part.strip() for part in _IDENTITY_HEADING_JOINER.split(_clean_heading(heading)) if part.strip()]
    return bool(parts) and all(_IDENTITY_HEADING_TERM.fullmatch(part) for part in parts)


def _is_lyric_heading(heading: str) -> bool:
    """Return true only for an explicit lyric/theme section heading.

    Looking for ``lyrics`` anywhere in an arbitrary line caused a vocal
    descriptor such as ``Vocals: English lyrics ...`` to disappear from the
    descriptor corpus. The parser deliberately trusts structured headings/labels,
    not incidental prose, when it separates retained lyric material.
    """

    return any(marker in _clean_heading(heading) for marker in _LYRIC_MARKERS)


def _split_descriptor_and_lyric_corpora(corpus: str) -> tuple[str, str]:
    """Separate identity, lyric, and musical-description retrieval corpora.

    Campaign outputs may retain lyric excerpts for audit. A word such as
    ``rock`` or ``drop`` inside a quoted lyric must never become a false
    genre/production tag, even though the same word is valid in a production
    section.
    Likewise, a model may echo source metadata such as ``Title: Rock`` or
    ``Artist: The Trap House``. Those identity fields must not be mistaken for
    genre/production descriptors. The raw analysis remains untouched;
    this only creates parser-local corpora.
    """

    descriptor_lines: list[str] = []
    lyric_lines: list[str] = []
    section_kind = "descriptor"
    for line in corpus.splitlines():
        heading_match = _LINE_HEADING.match(line)
        if heading_match:
            heading = heading_match.group(1)
            if _is_identity_heading(heading):
                section_kind = "identity"
            elif _is_lyric_heading(heading):
                section_kind = "lyric"
            else:
                section_kind = "descriptor"
        elif _INLINE_LYRIC_LABEL.match(line):
            section_kind = "lyric"

        if section_kind == "identity":
            continue
        if section_kind == "lyric":
            lyric_lines.append(line)
            continue
        # Strip direct quotations conservatively. They could be lyric excerpts
        # even when the producer did not label the surrounding sentence.
        descriptor_lines.append(_QUOTED_TEXT.sub(" ", line))
    return "\n".join(descriptor_lines), "\n".join(lyric_lines)


def _tempo_bucket(bpm: float) -> str:
    if bpm < 75:
        return "very slow"
    if bpm < 105:
        return "slow to midtempo"
    if bpm < 125:
        return "midtempo"
    if bpm < 150:
        return "upbeat"
    return "fast"


def extract_music_flamingo_metadata(raw_text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return deterministic tag and numeric-feature payloads for one analysis.

    Returned objects use the existing generic importer contract, making the
    same function safe for new campaign rows and later publisher backfills.
    """

    if not isinstance(raw_text, str) or not raw_text.strip():
        return [], []
    corpus = _corpus(raw_text)
    descriptor_corpus, lyric_corpus = _split_descriptor_and_lyric_corpora(corpus)
    tags: dict[tuple[str, str], dict[str, Any]] = {}
    # Identity headings were removed above. Section labels remain useful
    # retrieval keys, including lyric/theme labels.
    _extract_sections("\n".join((descriptor_corpus, lyric_corpus)), tags)
    for rule in _RULES:
        target_corpus = descriptor_corpus if rule.is_musical_descriptor else lyric_corpus
        if any(re.search(pattern, target_corpus, flags=re.IGNORECASE) for pattern in rule.patterns):
            _add_tag(tags, _tag_payload(rule))

    numeric_features: list[dict[str, Any]] = []
    bpm_values = sorted(
        {
            float(match.group(1))
            for match in _BPM.finditer(descriptor_corpus)
            if 20.0 <= float(match.group(1)) <= 300.0
        }
    )
    if len(bpm_values) == 1:
        bpm = bpm_values[0]
        numeric_features.append(
            {
                "name": "bpm",
                "value": bpm,
                "unit": "bpm",
                "confidence": 1.0,
                "source": PARSER_SOURCE,
            }
        )
        _add_tag(tags, _tag_payload(_rule("tempo", _tempo_bucket(bpm), rf"{bpm}", aliases=())))

    for match in _KEY.finditer(descriptor_corpus):
        note = match.group(1).upper()
        accidental = "#" if match.group(2) else ("b" if match.group(3) else "")
        quality = match.group(4).lower()
        _add_tag(
            tags,
            _tag_payload(
                _rule("harmony", f"key center {note}{accidental} {quality}", match.group(0))
            ),
        )

    return (
        [tags[key] for key in sorted(tags)],
        sorted(numeric_features, key=lambda feature: str(feature["name"])),
    )
