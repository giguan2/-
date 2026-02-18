from __future__ import annotations
from bs4 import BeautifulSoup
import html

import re as _re_simple
from telegram.error import BadRequest

# --- Time helpers ---
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    """Return timezone-aware KST datetime."""
    return datetime.now(KST)


def extract_simple_from_body(body: str) -> str:
    """서술형 body에서 '핵심 포인트 요약'을 한 줄로 압축해 반환.
    - [핵심 포인트 요약] / 핵심 포인트 요약 / 핵심포인트요약 등 다양한 표기 지원
    - HTML(<br>, <p>)이 섞여 있어도 처리
    """
    if not body:
        return ""

    text = str(body)

    # normalize newlines / HTML
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ")

    patterns = [
        r"\[\s*핵심\s*포인트\s*요약\s*\](.*?)(?:\n\s*[─\-]{5,}|\n\s*\[\s*최종\s*픽\s*\]|\n\s*\[|\Z)",
        r"핵심\s*포인트\s*요약\s*[:：]?\s*\n(.*?)(?:\n\s*[─\-]{5,}|\n\s*최종\s*픽\s*[:：]?|\n\s*\[|\Z)",
        r"핵심\s*포인트\s*[:：]?\s*\n(.*?)(?:\n\s*[─\-]{5,}|\n\s*최종\s*픽\s*[:：]?|\n\s*\[|\Z)",
        r"핵심포인트\s*요약\s*[:：]?\s*\n(.*?)(?:\n\s*[─\-]{5,}|\n\s*최종\s*픽\s*[:：]?|\n\s*\[|\Z)",
    ]

    section = ""
    for pat in patterns:
        m = re.search(pat, text, flags=re.S)
        if m:
            section = (m.group(1) or "").strip()
            if section:
                break

    # 섹션이 없으면 불릿 마지막 2~4개로 대체
    if not section:
        bullets = []
        for line in text.split("\n"):
            s = line.strip()
            if re.match(r"^[\-\•\*]+\s+", s):
                bullets.append(re.sub(r"^[\-\•\*]+\s*", "", s))
        if len(bullets) >= 2:
            section = "\n".join(bullets[-4:])

    if not section:
        return ""

    lines = []
    for line in section.split("\n"):
        s = line.strip()
        if not s:
            continue
        s = re.sub(r"^[\-\•\*]+\s*", "", s)
        lines.append(s)

        # 여러 줄 불릿을 "한 문장"으로 재작성 (가능하면 OpenAI 사용)
    cleaned = []
    for s in lines:
        s2 = s.strip()
        # 불필요한 끝 쉼표/공백 정리
        s2 = re.sub(r"\s*,\s*$", "", s2)
        s2 = re.sub(r"\s+", " ", s2).strip()
        if s2:
            cleaned.append(s2)

    if not cleaned:
        return ""

    # OpenAI로 1문장 재작성 (불릿이 2개 이상일 때만)
    client_oa = get_openai_client()
    if client_oa and len(cleaned) >= 2:
        try:
            bullets_txt = "\n".join([f"- {b}" for b in cleaned[:6]])
            prompt = f'''아래 "핵심 포인트 요약" 불릿들을 의미를 유지한 채 자연스러운 한국어 1문장으로 재작성해줘.

조건:
- 반드시 한 문장(줄바꿈 없이)
- 어색한 쉼표 나열 금지 (필요한 최소만 사용)
- 관형형(예: "한", "미친", "준")으로 끝나지 않게 종결
- 220자 이내
- 출력은 문장만 (따옴표/머리말/불릿 금지)

불릿:
{bullets_txt}
'''
            resp = client_oa.chat.completions.create(
                model=os.getenv("SIMPLE_REWRITE_MODEL", "gpt-4.1-mini"),
                messages=[
                    {"role": "system", "content": "Rewrite Korean sports analysis bullet points into one natural sentence."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
            )
            one = (resp.choices[0].message.content or "").strip()
            one = re.sub(r"\s+", " ", one).strip()
            # 안전장치: 너무 길거나 비어있으면 폴백
            if (not one) or (len(one) > 240):
                raise ValueError("simple rewrite empty/too long")
        except Exception:
            one = " | ".join(cleaned)
    else:
        one = " | ".join(cleaned)

    one = re.sub(r"\s+", " ", one).strip()

    if len(one) > 220:
        one = one[:217] + "..."
    # 마지막에 최종 픽을 붙인다(줄바꿈 유지)
    try:
        pick_block = extract_final_pick_from_body(body)
    except Exception:
        pick_block = ""
    if pick_block:
        one2 = (one or "").strip()
        if one2:
            return one2 + "\n\n" + pick_block
        return pick_block

    return one


# --- Dynamic cafe simple builder (for export sheet G column) ---
import re
import hashlib as _hashlib
import random as _random

_KEYWORD_TAGS = [
    ("토토", "#토토"),
    ("프로토", "#프로토"),
    ("스포츠토토", "#스포츠토토"),
    ("토토분석", "#토토분석"),
("세트피스", "#세트피스"),
    ("역습", "#역습"),
    ("압박", "#압박"),
    ("측면", "#측면"),
    ("수비", "#수비"),
    ("공격", "#공격"),
    ("제공권", "#제공권"),
    ("전환", "#전환"),
    ("점유", "#점유"),
    ("피지컬", "#피지컬"),
    ("범실", "#범실"),
    ("블로킹", "#블로킹"),
]

_SPORT_TAGS = {
    "축구": ["#축구분석", "#해외축구"],
    "K리그": ["#축구분석", "#K리그"],
    "J리그": ["#축구분석", "#J리그"],
    "야구": ["#야구분석", "#해외야구"],
    "농구": ["#농구분석", "#해외농구"],
    "배구": ["#배구분석", "#V리그"],
}

# =====================================================
# Team name display normalization (SAFE, editable)
# - 목적: export_* 제목/키워드에서 'vs/대' 제거 + FC/CF/워리어스 같은 접미어 제거
# - ⚠️ 크롤링/업로드(네이버 API) 로직은 건드리지 않고, '표시용 문자열'만 정리합니다.
# - 나중에 빼야 할 단어가 더 생기면 아래 리스트에만 추가하면 됩니다.
# =====================================================

# --- Soccer (축구) ---
TEAM_SUFFIX_SOCCER = [
    # 유럽/남미 등 약어
    " FC", " CF", " SC", " AFC", " FK", " SK", " AC", " CD",
    # 필요하면 여기에 계속 추가
]

# --- Basketball (농구) ---
TEAM_SUFFIX_BASKETBALL = [
    # NBA/KBL 등에서 도시+마스코트 형태일 때, 마스코트(별칭) 제거용
    # ✅ 필요할 때 아래 리스트에 " 별칭" 형태로 계속 추가하면 됩니다.
    " 울브스", " 팀버울브스",
    " 워리어스",
    " 레이커스",
    " 클리퍼스",
    " 셀틱스",
    " 불스",
    " 히트",
    " 호크스",
    " 호네츠", " 호넷츠",
    " 매직",
    " 위저즈",
    " 랩터스",
    " 네츠", " 넷츠",
    " 캐벌리어스", " 캐벌리어즈",
    " 페이서스",
    " 피스톤스",
    " 스퍼스",
    " 킹스",
    " 그리즐리스", " 그리즐리즈",
    " 로켓츠", " 로케츠",
    " 매버릭스",
    " 선즈",
    " 너기츠",
    " 재즈",
    " 썬더", " 선더",
    " 펠리컨스",
    " 블레이저스", " 트레일블레이저", " 트레일블레이저스",
    " 식서스", " 세븐티식서스",
    " 벅스",
    " 닉스",
    # 필요하면 여기에 계속 추가
]


# --- Baseball (야구) ---
TEAM_SUFFIX_BASEBALL = [
    # MLB 예시
    " 다저스", " 양키스", " 레드삭스", " 화이트삭스", " 자이언츠", " 컵스",
    " 파드리스", " 애스트로스", " 브레이브스", " 메츠", " 오리올스", " 레인저스",
    " 블루제이스", " 로열스", " 말린스", " 필리스", " 내셔널스", " 트윈스", " 가디언스",
    # KBO/NPB 등(필요시 추가)
    " 이글스", " 타이거즈", " 라이온즈", " 베어스", " 히어로즈", " 랜더스", " 자이언츠",
    # 필요하면 여기에 계속 추가
]

# --- Volleyball (배구) ---
TEAM_SUFFIX_VOLLEYBALL = [
    # 배구는 구단명이 곧 키워드인 경우가 많아 기본은 비워두고, 필요시만 추가 추천
    # 예: " 스카이워커스", " 점보스" 등
]

def normalize_team_name_by_sport(name: str, sport_key: str) -> str:
    """표시용 팀명 정규화(접미어 제거).
    sport_key: soccer | basketball | baseball | volleyball
    """
    t = (name or "").strip()
    if not t:
        return ""

    sk = (sport_key or "").strip().lower()
    if sk == "soccer":
        suffixes = TEAM_SUFFIX_SOCCER
    elif sk == "basketball":
        suffixes = TEAM_SUFFIX_BASKETBALL
    elif sk == "baseball":
        suffixes = TEAM_SUFFIX_BASEBALL
    elif sk == "volleyball":
        suffixes = TEAM_SUFFIX_VOLLEYBALL
    else:
        return t

    for s in suffixes:
        if s and t.endswith(s):
            return t[: -len(s)].strip()

    return t

def infer_norm_sport_key(sport_label: str, row_sport: str, league: str = "") -> str:
    """export row의 sport/league 정보를 보고 팀명 정규화에 쓸 sport_key를 추정."""
    sl = (sport_label or "").strip()
    rs = (row_sport or "").strip()
    lg = (league or "").strip()

    # 1) 상위 sport_label 우선
    if "축구" in sl:
        return "soccer"
    if "야구" in sl:
        return "baseball"
    if "배구" in sl:
        return "volleyball"
    if "농구" in sl:
        # 농구/배구 혼합 라우트일 때는 row_sport/league로 배구를 분리
        if any(x in rs for x in ["V리그", "배구"]) or any(x in lg for x in ["V리그", "배구", "VOLLEY"]):
            return "volleyball"
        return "basketball"

    # 2) row_sport 기반 폴백
    if any(x in rs for x in ["축구", "K리그", "J리그", "해외축구"]):
        return "soccer"
    if any(x in rs for x in ["야구", "KBO", "NPB", "MLB", "해외야구"]):
        return "baseball"
    if any(x in rs for x in ["배구", "V리그"]):
        return "volleyball"
    if any(x in rs for x in ["농구", "NBA", "KBL", "WKBL", "WNBA"]):
        return "basketball"

    return ""

def build_matchup_display(home_raw: str, away_raw: str, sport_key: str) -> tuple[str, str, str]:
    """(home_disp, away_disp, matchup_display) 반환. matchup_display는 '팀1 팀2' 형태."""
    home_disp = normalize_team_name_by_sport(home_raw, sport_key)
    away_disp = normalize_team_name_by_sport(away_raw, sport_key)
    matchup_display = " ".join([x for x in [home_disp, away_disp] if x]).strip()
    return home_disp, away_disp, matchup_display

def build_export_title(target_date, league: str, home_raw: str, away_raw: str, sport_key: str) -> str:
    """export_* D열(title) 표준화: 'M월 D일 [리그] 홈 원정 스포츠분석'"""
    mm = getattr(target_date, "month", "")
    dd = getattr(target_date, "day", "")
    home_disp, away_disp, matchup = build_matchup_display(home_raw, away_raw, sport_key)
    lg = (league or "").strip()
    # league가 비어있으면 대괄호는 생략
    if lg:
        return f"{mm}월 {dd}일 [{lg}] {matchup} 스포츠분석".strip()
    return f"{mm}월 {dd}일 {matchup} 스포츠분석".strip()



# =====================================================
# Korean particle(josa) fixer (SAFE)
# - 팀명 접미어(로켓츠/그리즐리즈 등)를 제거한 뒤 "휴스턴는/휴스턴가"처럼
#   조사가 어색해지는 문제를 자동 보정합니다.
# - 크롤링/업로드 로직은 건드리지 않고, '텍스트 출력'만 수정합니다.
# =====================================================

_JOSA_GROUP = {
    "은": "topic", "는": "topic",
    "이": "subject", "가": "subject",
    "을": "object", "를": "object",
    "과": "and", "와": "and",
}

def _has_batchim(ch: str) -> bool:
    """한글 음절의 받침 유무(True=받침 있음)."""
    if not ch:
        return False
    c = ord(ch)
    if 0xAC00 <= c <= 0xD7A3:
        return (c - 0xAC00) % 28 != 0
    return False

def _last_hangul_char(s: str) -> str | None:
    """문자열의 마지막 한글 음절을 찾음(없으면 None)."""
    if not s:
        return None
    for ch in reversed(s):
        o = ord(ch)
        if 0xAC00 <= o <= 0xD7A3:
            return ch
    return None

# 단어+조사 패턴 (문장부호/공백/끝에서만 매치)
_JOSA_RE = re.compile(r"([가-힣A-Za-z0-9]+)(은|는|이|가|을|를|과|와)(?=[^가-힣A-Za-z0-9]|$)")

def _fix_korean_josa(text: str) -> str:
    """텍스트 내 조사(은/는, 이/가, 을/를, 과/와)를 받침 규칙에 맞게 자동 교정."""
    if not text:
        return ""

    def _fix_line(line: str) -> str:
        # 해시태그 라인은 건드리지 않음(태그 훼손 방지)
        if line.lstrip().startswith("#"):
            return line

        def repl(m: re.Match) -> str:
            word = m.group(1)
            josa = m.group(2)

            last = _last_hangul_char(word)
            if not last:
                return m.group(0)

            has_b = _has_batchim(last)
            grp = _JOSA_GROUP.get(josa)

            if grp == "topic":
                want = "은" if has_b else "는"
            elif grp == "subject":
                want = "이" if has_b else "가"
            elif grp == "object":
                want = "을" if has_b else "를"
            elif grp == "and":
                want = "과" if has_b else "와"
            else:
                return m.group(0)

            return word + want

        return _JOSA_RE.sub(repl, line)

    return "\n".join(_fix_line(ln) for ln in str(text).splitlines())

def _postprocess_site_body_text(text: str, *, footer_line: str = "") -> str:
    """
    사이트용 body(E열) 최종 후처리(⚠️ 안전장치):
    - '고트티비/GOATTV/goat-tv' 등 브랜드/사이트명 언급 제거(띄어쓰기/하이픈/대소문자 변형 포함)
    - 매치업 구분자(vs/VS/v.s./대) 제거 → 공백으로 연결
    - 개행 구조는 유지하면서 불필요한 공백만 정리
    - (선택) footer_line을 해시태그 블록 앞(있으면) 또는 끝에 1회 삽입

    ✅ 목적:
    OpenAI 리라이팅/원문 내에 브랜드명이 섞여 들어와도 export 시트(E열)와 업로드 본문에서
    절대 노출되지 않도록 '마지막 단계'에서 한번 더 걸러준다.
    """
    if not text:
        out = ""
    else:
        out = str(text)

    # 1) 브랜드/사이트명 제거(한글/영문/하이픈/띄어쓰기 변형 포함)
    #   - "고트티비", "고트 티비", "고트-티비", "고트TV" 등
    out = re.sub(
        r"(고트\s*[-_]?\s*티비|고트티비|고트\s*TV)\s*(?:\.com)?\s*"
        r"(?:의|에서도|에서|도|을|를|은|는|이|가|에|와|과)?",
        "",
        out,
        flags=re.I,
    )
    #   - "GOATTV", "GOAT TV", "goat-tv.com" 등
    out = re.sub(r"(GOAT\s*TV|GOATTV|GOAT[-_\s]*TV|goat[-_\s]*tv(?:\.com)?)", "", out, flags=re.I)

    # 1-b) 브랜드 제거 후 어색한 접속/조사 흔적 최소 보정(과도한 문장 변형은 피함)
    out = out.replace("스포츠분석과 정보를", "스포츠분석 정보를")
    out = out.replace("스포츠분석과 자료를", "스포츠분석 자료를")
    out = out.replace("스포츠분석과 자료", "스포츠분석 자료")

    # 2) 매치업 구분자 제거: ' A vs B ' / 'AvsB' 모두 대응(개행은 건드리지 않음)
    out = re.sub(r"(?i)\s+(?:vs\.?|v\.s\.|V\.S\.)\s+", " ", out)
    out = re.sub(r"(?i)(?<=\S)(?:vs\.?|v\.s\.|V\.S\.)(?=\S)", " ", out)
    out = re.sub(r"\s+대\s+", " ", out)

    # 3) 라인별 공백 정리(개행 유지)
    lines = []
    for line in out.splitlines():
        line = re.sub(r"[ \t]{2,}", " ", line).rstrip()
        # 라인 전체가 불필요한 구두점/구분선만 남는 경우 최소 정리
        line = re.sub(r"^[\-─]{5,}$", "───────────────", line).rstrip()
        lines.append(line)
    out = "\n".join(lines).strip()

    # 3-b) 팀명 치환 후 '휴스턴는/휴스턴가' 같은 조사 어색함 자동 보정
    out = _fix_korean_josa(out)

    # 4) footer 삽입(원하면)
    footer_line = (footer_line or "").strip()
    if footer_line and footer_line not in out:
        lines = out.splitlines()

        # trailing empty lines cut
        i = len(lines)
        while i > 0 and lines[i - 1].strip() == "":
            i -= 1

        # trailing hashtag block
        j = i
        while j > 0 and lines[j - 1].lstrip().startswith("#"):
            j -= 1

        if j < i:
            # insert footer before hashtags
            head = lines[:j]
            tail = lines[j:]
            while head and head[-1].strip() == "":
                head.pop()
            if head:
                head.append("")
            head.append(footer_line)
            head.append("")
            out = "\n".join(head + tail).strip()
        else:
            out = (out.rstrip() + "\n\n" + footer_line).strip()

    return out



# =====================================================
# Matchup keyword injection in body(E열) (SAFE)
# - 목적: 본문에도 "팀1 팀2 경기분석/스포츠분석" 키워드가 자연스럽게 1회 녹아들도록 함
# - ⚠️ 크롤링/업로드 로직은 건드리지 않고, body 텍스트 후처리만 수행
# - 기본은 "경기분석" 사용. 필요하면 Render 환경변수로 변경 가능:
#     BODY_MATCH_KEYWORD_WORD=스포츠분석   (또는 경기분석)
#     BODY_MATCH_KEYWORD_WORD=OFF         (비활성화)
# =====================================================

_BODY_MATCH_HEADERS = {
    "[경기 흐름 전망]", "[경기흐름 전망]",
    "[경기 흐름전망]", "[경기흐름전망]",
    "[경기 전망]", "[경기전망]",
}

def _get_body_match_keyword_word() -> str:
    v = (os.getenv("BODY_MATCH_KEYWORD_WORD") or "").strip()
    if not v:
        return "경기분석"
    if v.upper() in {"OFF", "0", "FALSE", "NO", "NONE"}:
        return ""
    return v

def _inject_match_keyword(text: str, matchup: str, *, keyword_word: str) -> str:
    """
    본문 텍스트에 "이번 {팀1 팀2} {키워드}에서는 ..." 문구를 1회 자연스럽게 삽입/치환.
    - 우선순위: [경기 흐름 전망] 섹션 첫 문장("이번 경기는 ...")을 치환
    - 이미 '{팀1 팀2} (경기분석|스포츠분석)'이 존재하면 중복 삽입하지 않음
    """
    if not text:
        return ""
    if not matchup or not keyword_word:
        return str(text)

    out = str(text)

    # 이미 키워드가 들어가 있으면 그대로
    if re.search(rf"{re.escape(matchup)}\s*(?:경기분석|스포츠분석)", out):
        return out

    def _rewrite_first_sentence(line: str) -> str:
        # "이번 경기는 ..." → "이번 팀1 팀2 경기분석에서는 ..."
        new = re.sub(r"^이번\s*경기는\s*", f"이번 {matchup} {keyword_word}에서는 ", line)
        if new != line:
            return new
        # "이번 경기에선/에서는 ..." 패턴
        new = re.sub(r"^이번\s*경기(?:\s*분석)?(?:에선|에서는|에선)\s*", f"이번 {matchup} {keyword_word}에서는 ", line)
        if new != line:
            return new
        # 그 외: 앞에 자연스럽게 프리픽스
        return f"이번 {matchup} {keyword_word}에서는 {line.lstrip()}"

    lines = out.splitlines()

    # 1) [경기 흐름 전망] 섹션에서 삽입/치환
    header_idx = None
    for i, ln in enumerate(lines):
        if ln.strip() in _BODY_MATCH_HEADERS:
            header_idx = i
            break

    if header_idx is not None:
        j = header_idx + 1
        while j < len(lines) and lines[j].strip() == "":
            j += 1
        if j < len(lines) and lines[j].strip():
            lines[j] = _rewrite_first_sentence(lines[j])
            return "\n".join(lines)

    # 2) 섹션이 없으면: 첫 "이번 경기는" 라인을 찾아 치환
    for i, ln in enumerate(lines):
        if re.match(r"^\s*이번\s*경기는\s*", ln):
            lines[i] = _rewrite_first_sentence(ln.strip())
            return "\n".join(lines)

    # 3) 그래도 없으면: 첫 내용 라인(헤더 제외)에 1회 삽입
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        if re.match(r"^\[[^\]]+\]$", s):
            continue
        lines[i] = _rewrite_first_sentence(ln)
        return "\n".join(lines)

    return out


def normalize_text_teamnames(text: str, *, sport_key: str, home_raw: str, away_raw: str) -> str:
    """본문(body)에서 팀명/구분자 표기를 표시용으로 정리.
    - raw 팀명 → 정규화된 팀명
    - '팀1 vs 팀2' / '팀1 대 팀2' 형태 → '팀1 팀2'
    """
    if not text:
        return ""
    out = str(text)

    home_disp, away_disp, matchup = build_matchup_display(home_raw, away_raw, sport_key)

    # 1) raw 팀명 치환(존재할 때만)
    if home_raw and home_disp and home_raw != home_disp:
        out = out.replace(home_raw, home_disp)
    if away_raw and away_disp and away_raw != away_disp:
        out = out.replace(away_raw, away_disp)

    # 2) 구분자 제거(팀명 기준으로만 타겟팅)
    #    - 팀명이 바뀐 후(out.replace)에도 남아있는 구분자 패턴을 정리
    try:
        if home_disp and away_disp:
            pat = re.compile(rf"{re.escape(home_disp)}\s*(?:vs|VS|대|v\.s\.|V\.S\.|—|–|-)\s*{re.escape(away_disp)}")
            out = pat.sub(matchup, out)
        if home_raw and away_raw:
            pat2 = re.compile(rf"{re.escape(home_raw)}\s*(?:vs|VS|대|v\.s\.|V\.S\.|—|–|-)\s*{re.escape(away_raw)}")
            out = pat2.sub(matchup, out)
    except Exception:
        pass

    kw_word = _get_body_match_keyword_word()
    if kw_word:
        out = _inject_match_keyword(out, matchup, keyword_word=kw_word)

    return _postprocess_site_body_text(out)

def _stable_rng(seed: str) -> _random.Random:
    h = _hashlib.md5((seed or "seed").encode("utf-8")).hexdigest()
    return _random.Random(int(h[:8], 16))


# =====================================================
# Site footer line (export_* E열 body 하단 고정 문구) - variation helper
# - 목적: '스포츠분석 커뮤니티 오분' 키워드는 유지하되, 매번 동일한 문장 반복을 피한다.
# - (옵션) 환경변수:
#     SITE_FOOTER_LINE:     단일 문구 강제(기존 동작 유지). "off/0/false/none/no"면 비활성.
#     SITE_FOOTER_VARIANTS: '||' 또는 줄바꿈으로 여러 문구 지정 (각 항목에 {KW} 사용 가능)
# =====================================================

_SITE_FOOTER_KW = "스포츠분석 커뮤니티 오분"

_DEFAULT_SITE_FOOTER_VARIANTS = [
    f"최종 판단은 라인업·부상·로테이션 변수까지 반영해야 하니, {_SITE_FOOTER_KW}의 업데이트를 함께 확인하는 편이 안전하다.",
    f"전술 상성, 템포, 세트피스 같은 승부처는 {_SITE_FOOTER_KW}에서 더 촘촘히 정리해두었으니 참고하면 도움이 된다.",
    f"데이터 흐름과 최근 경기 맥락을 함께 보면 해석이 더 선명해지므로, {_SITE_FOOTER_KW}의 분석 자료도 같이 확인해보자.",
    f"표면적인 결과보다 과정(찬스 질·전환·압박 효율)을 보는 게 중요하니, {_SITE_FOOTER_KW}에서 추가 지표를 참고하면 좋다.",
    f"경기 전 변수(일정, 체력, 로테이션) 체크는 필수라서, {_SITE_FOOTER_KW}의 최신 정리를 함께 보길 권한다.",
    f"매치업의 핵심은 중원 간격과 뒷공간 관리 같은 디테일에 있으니, {_SITE_FOOTER_KW}의 관전 포인트도 참고하자.",
    f"예측은 ‘누가 더 잘하나’보다 ‘어디서 깨지나’를 보는 싸움이라, {_SITE_FOOTER_KW}의 리스크 포인트 정리도 유용하다.",
    f"전력 비교를 할 때는 상대 전술 대응까지 봐야 하니, {_SITE_FOOTER_KW}에서 정리한 변수 체크를 함께 확인하자.",
    f"경기 흐름을 좌우하는 키는 초반 압박과 세컨볼인데, {_SITE_FOOTER_KW}에서 관련 장면을 더 자세히 짚어볼 수 있다.",
    f"핵심 포인트는 단순 승패가 아니라 득점 루트와 실점 리스크이니, {_SITE_FOOTER_KW}의 추가 분석을 참고하면 좋다.",
    f"라인업이 확정되면 해석이 달라질 수 있으므로, {_SITE_FOOTER_KW}의 프리뷰/업데이트를 함께 보는 걸 추천한다.",
    f"전술 변화나 포지션 조정 한 번에 경기 양상이 바뀔 수 있어, {_SITE_FOOTER_KW}의 심화 코멘트도 도움이 된다.",
    f"수비 라인 컨트롤·전환 속도 같은 요소는 수치로도 드러나니, {_SITE_FOOTER_KW}의 데이터 정리도 같이 확인해보자.",
    f"승부처는 ‘결정력’보다 ‘실수 관리’가 되는 경우가 많아, {_SITE_FOOTER_KW}에서 변수 리마인드를 해두면 좋다.",
    f"맞대결의 포인트를 한 문장으로 정리하면 ‘상대 강점을 얼마나 지우느냐’이니, {_SITE_FOOTER_KW}의 대응 포인트도 참고하자.",
    f"경기 전 체크리스트(부상, 징계, 일정, 컨디션)는 {_SITE_FOOTER_KW}에서 업데이트되니 함께 확인하면 도움이 된다.",
    f"전개 속도와 전환 타이밍이 관건인 매치업은 {_SITE_FOOTER_KW}의 흐름 분석을 같이 보면 이해가 쉬워진다.",
    f"정답은 없지만 근거는 쌓을 수 있으니, {_SITE_FOOTER_KW}의 근거 중심 정리로 한 번 더 점검해보자.",
    f"이 경기는 변수가 많아 단정하기 어렵다. {_SITE_FOOTER_KW}의 최신 정보까지 반영해 판단하면 더 안정적이다.",
    f"전술 상성/교체 카드/세트피스처럼 ‘숨은 변수’는 {_SITE_FOOTER_KW}에서 더 자세히 다루고 있으니 참고하자.",
    f"상대 전술에 대한 1차 대응(압박 회피, 뒷공간 커버)은 {_SITE_FOOTER_KW}에서 구체적으로 정리돼 있다.",
    f"선발 변화 하나로 득점 루트가 달라질 수 있어, {_SITE_FOOTER_KW}의 라인업 체크와 함께 보면 좋다.",
    f"핵심은 ‘흐름을 언제 가져오느냐’이니, {_SITE_FOOTER_KW}에서 경기 운영 포인트도 같이 확인해보자.",
    f"분석의 완성은 마지막 변수 확인이다. {_SITE_FOOTER_KW}의 업데이트를 참고하면 판단에 도움이 될 것이다.",
]

def _pick_site_footer_line(*, seed: str = "") -> str:
    """하단 고정 문구를 자연스럽게 변형해 반환(키워드 고정).

    - 기본 키워드: '스포츠분석 커뮤니티 오분' 포함(SEO)
    - SITE_FOOTER_VARIANTS가 있으면 커스텀 문구를 우선 사용
      - 구분자: '||' 또는 줄바꿈
      - 플레이스홀더: {KW}
    - seed를 주면 매치업/기사별로 '안정적으로' 분산(재현성)
    """
    kw = _SITE_FOOTER_KW
    raw = (os.getenv("SITE_FOOTER_VARIANTS") or "").strip()

    variants: list[str] = []
    if raw:
        parts = [p.strip() for p in re.split(r"\|\||\n+", raw) if p.strip()]
        for p in parts:
            p2 = p.replace("{KW}", kw).strip()
            if not p2:
                continue
            if kw not in p2:
                # 키워드가 포함되지 않으면 제외(의도치 않은 문구 방지)
                continue
            if not re.search(r"[\.!\?…]$", p2):
                p2 += "."
            variants.append(p2)

    if not variants:
        variants = list(_DEFAULT_SITE_FOOTER_VARIANTS)

    if not variants:
        return ""

    if seed:
        try:
            rng = _stable_rng(seed)
            return rng.choice(variants)
        except Exception:
            pass

    # seed가 없으면 일반 랜덤(프로세스 단위)
    try:
        return _random.choice(variants)
    except Exception:
        return variants[0]


def _extract_league_bracket(title: str) -> str:
    m = re.search(r"\[([^\]]{2,30})\]", title or "")
    return m.group(1).strip() if m else ""

def _extract_matchup(title: str) -> tuple[str, str, str]:
    t = (title or "").replace("\u200b", "").strip()
    # capture full away team (including spaces) until "스포츠분석" or end
    m = re.search(r"(.+?)\s+vs\s+(.+?)(?:\s+스포츠분석\b|$)", t)
    if not m:
        return ("", "", "")
    home = m.group(1).strip()
    away = m.group(2).strip()

    # home 쪽에 날짜/리그가 같이 들어오는 케이스 정리
    home = re.sub(r"^\d+월\s*\d+일\s*", "", home)
    home = re.sub(r"^\[[^\]]+\]\s*", "", home)
    home = home.replace("스포츠분석", "").strip()

    away = away.replace("스포츠분석", "").strip()

    matchup = f"{home} vs {away}"
    return (home, away, matchup)

def _slug_tag(s: str) -> str:
    s = re.sub(r"[\[\]\(\)<>\"'`]", "", s or "")
    s = re.sub(r"스포츠분석", "", s)
    s = s.replace("vs", " ").replace("VS", " ")
    s = re.sub(r"\s+", "", s)
    # 날짜/숫자 제거(1월25일 같은 초장문 태그 방지)
    s = re.sub(r"[0-9]+", "", s)
    s = s.replace("월", "").replace("일", "")
    s = re.sub(r"[^\w가-힣]+", "", s)
    # 너무 긴 경우 잘라내기
    return s[:15]
def build_dynamic_cafe_simple(title: str, body: str, *, sport: str = "", seed: str = "", home_team: str = "", away_team: str = "") -> str:
    """export_* 시트 G열(simple) 생성(요약형)
    목표:
    - body 전체 복사 금지
    - [핵심 포인트 요약]이 있으면 1문장 요약, 없으면 본문에서 1~2문장 요약
    - 제목(또는 매치업)은 도입/결론에서만 자연스럽게 사용(과도 반복 금지)
    - [최종 픽]은 1회만 포함
    - 하단: 해시태그(토토/프로토/스포츠토토/토토분석 포함 가능)
    """
    title = (title or "").strip()
    body = (body or "").strip()

    league = _extract_league_bracket(title)
    home, away, matchup = _extract_matchup(title)
    # ✅ title에 'vs/대'가 없더라도 팀 태그/키워드를 유지하기 위해,
    #    호출부에서 home_team/away_team(정규화된 표시용 팀명)을 넘겨주면 우선 사용한다.
    ht_override = (home_team or "").strip()
    at_override = (away_team or "").strip()
    if ht_override and at_override:
        home, away = ht_override, at_override
        matchup = f"{home} {away}"


    # fallback matchup (날짜/리그/스포츠분석 제거)
    if not matchup:
        t = re.sub(r"^\d+월\s*\d+일\s*", "", title)
        t = re.sub(r"^\[[^\]]+\]\s*", "", t)
        t = t.replace("스포츠분석", "").strip()
        matchup = t

    # 본문에서 최종 픽 섹션 제거한 텍스트로 요약/키워드 추출(픽 중복 방지)
    def _remove_pick_section(txt: str) -> str:
        if not txt:
            return ""
        x = txt
        x = x.replace("\r\n", "\n").replace("\r", "\n")
        x = re.sub(r"<br\s*/?>", "\n", x, flags=re.I)
        x = re.sub(r"</(p|div|li)>", "\n", x, flags=re.I)
        x = re.sub(r"<[^>]+>", "", x)
        x = x.replace("&nbsp;", " ")
        # [최종 픽] 이후 끝까지 제거
        x = re.sub(r"\[\s*최종\s*픽\s*\][\s\S]*$", "", x, flags=re.I)
        x = re.sub(r"최종\s*픽\s*[:：]?[\s\S]*$", "", x, flags=re.I)
        return x.strip()

    body_nopick = _remove_pick_section(body)

    rng = _stable_rng(seed or (title + "|" + matchup + "|" + body_nopick[:80]))

    # theme: 본문에서 키워드 탐색, 없으면 후보에서 선택
    theme = ""
    for k, _tg in _KEYWORD_TAGS:
        if k and k in body_nopick:
            theme = k
            break
    if not theme:
        theme = rng.choice(["세트피스", "압박", "전환", "수비", "역습", "피지컬"])

    # 요약: 핵심포인트 1문장(없으면 본문 첫 문장들)
    core_one = (extract_simple_from_body(body_nopick) or "").strip()

    # 요약에 픽 관련 라인 들어가면 제거
    if core_one:
        lines = []
        for ln in core_one.split("\n"):
            t = ln.strip()
            if not t:
                continue
            if any(x in t for x in ["승패", "핸디", "언오버", "최종 픽"]):
                continue
            lines.append(t)
        core_one = " ".join(lines).strip()

    if not core_one:
        # 본문에서 첫 문단(헤더/구분선 제외) 1~2문장 추출
        paras = []
        for block in re.split(r"\n\s*\n+", body_nopick):
            b = block.strip()
            if not b:
                continue
            if re.match(r"^[─\-]{5,}$", b):
                continue
            if re.match(r"^\[.*?\]$", b):
                continue
            if b.startswith("[") and b.endswith("]") and len(b) <= 30:
                continue
            paras.append(b)
        base = paras[0] if paras else body_nopick
        base = re.sub(r"\s+", " ", base).strip()
        # 문장 1~2개
        sents = re.split(r"(?<=[\.\!\?다])\s+", base)
        core_one = " ".join([x.strip() for x in sents[:2] if x.strip()])[:320].strip()

    # 픽 블록(1회)
    pick_block = (extract_final_pick_from_body(body) or "").strip()

    intro_pool = [
        "{title}은 초반 주도권 싸움이 포인트다.",
        "{title}은 전술 상성에서 승부가 갈릴 수 있다.",
        "{title}은 {theme} 대응이 결과를 좌우할 수 있다.",
        "{title}은 운영 안정감이 먼저 중요한 경기다.",
        "{title}은 흐름 싸움에서 우위가 갈릴 수 있다.",
        "{title}은 한두 장면의 완성도가 관건이다.",
    ]
    outro_pool_by_theme = {
        "세트피스": [
            "정리하면 {matchup} 경기는 세트피스 한두 장면이 승부처가 될 수 있다.",
            "끝으로 {matchup} 경기는 세트피스 수비/공격 완성도가 결과를 가를 가능성이 크다.",
            "결국 {matchup} 경기는 세트피스에서 기회를 더 많이 쌓는 쪽이 유리하다.",
            "{matchup} 경기는 코너킥·프리킥 한 번의 디테일이 점수로 이어질 수 있다.",
        ],
        "압박": [
            "요약하면 {matchup} 경기는 압박 타이밍과 라인 간격이 포인트다.",
            "결국 {matchup} 경기는 중원 압박이 먼저 먹히는 쪽이 흐름을 잡을 수 있다.",
            "마지막으로 {matchup} 경기는 압박 회피와 전진 패스의 정확도가 관건이다.",
            "{matchup} 경기는 강하게 눌렀을 때의 세컨볼 대응이 승부를 가를 수 있다.",
        ],
        "전환": [
            "정리하면 {matchup} 경기는 공수 전환 속도에서 차이가 날 수 있다.",
            "결국 {matchup} 경기는 전환 한 번에 수비 라인이 무너질 수 있어 실점 관리가 중요하다.",
            "끝으로 {matchup} 경기는 전환 상황에서 뒷공간 대응이 승부처다.",
            "{matchup} 경기는 전환 국면에서 실수를 줄이는 쪽이 유리하다.",
        ],
        "수비": [
            "마무리로 {matchup} 경기는 실점 관리와 박스 안 대응이 핵심이다.",
            "결국 {matchup} 경기는 수비 조직력 유지가 먼저 시험대에 오른다.",
            "정리하면 {matchup} 경기는 라인 컨트롤과 커버 타이밍이 승부처가 될 수 있다.",
            "{matchup} 경기는 수비 실수 한 번이 그대로 점수로 이어질 수 있다.",
        ],
        "역습": [
            "정리하면 {matchup} 경기는 역습 한 방의 완성도가 승부를 가를 수 있다.",
            "끝으로 {matchup} 경기는 역습 전개 속도와 마무리의 정확도가 관건이다.",
            "결국 {matchup} 경기는 역습 찬스를 더 효율적으로 살리는 쪽이 유리하다.",
            "{matchup} 경기는 역습 상황에서 첫 패스 선택이 승부처가 될 수 있다.",
        ],
        "피지컬": [
            "마지막으로 {matchup} 경기는 피지컬 경합과 세컨볼 싸움에서 흐름이 갈릴 수 있다.",
            "정리하면 {matchup} 경기는 제공권·경합에서 우위가 승부를 가를 가능성이 크다.",
            "결국 {matchup} 경기는 강도 높은 몸싸움에서 버티는 쪽이 유리하다.",
            "{matchup} 경기는 후반 체력 싸움에서 격차가 벌어질 수 있다.",
        ],
    }

    outro_pool_generic = [
        "정리하면 {matchup} 경기는 운영 디테일에서 승부가 갈릴 수 있다.",
        "결국 {matchup} 경기는 한두 장면의 완성도가 결과를 좌우할 수 있다.",
        "마무리로 {matchup} 경기는 실수 관리가 중요하다.",
        "{matchup} 경기는 후반 운영에서 차이가 날 수 있다.",
        "{matchup} 경기는 흐름을 먼저 잡는 팀이 유리하다.",
        "끝으로 {matchup} 경기는 결정력과 실점 관리가 함께 중요하다.",
    ]

    def _choose_outro() -> str:
        pool = outro_pool_by_theme.get(theme, []) + outro_pool_generic
        return rng.choice(pool)


    def _fill(tpl: str) -> str:
        return tpl.format(title=title, matchup=matchup, theme=theme)

    intro = _fill(rng.choice(intro_pool)).strip()
    outro = _fill(_choose_outro()).strip()

    # 해시태그(짧고 자연스럽게)
    tags = ["#스포츠분석"]

    # 토토/프로토 계열은 과도 반복 피하고 1~2개만 선택(안정적으로)
    betting_pool = ["#토토", "#프로토", "#스포츠토토", "#토토분석"]
    rng.shuffle(betting_pool)
    tags += betting_pool[:2]

    tags += _SPORT_TAGS.get(sport, ["#해외스포츠"])

    if league:
        tg = "#" + _slug_tag(league)
        if len(tg) >= 2:
            tags.append(tg)

    # 팀 태그는 실패 추출로 초장문이 나오면 제외
    def _safe_team_tag(name: str):
        if not name:
            return None
        n = name.strip()
        if any(x in n for x in ["월", "일", "[", "]", "스포츠분석", "vs", "VS"]):
            return None
        if len(n) > 18:
            return None
        slug = _slug_tag(n)
        if not slug or len(slug) < 2:
            return None
        return "#" + slug

    ht = _safe_team_tag(home)
    at = _safe_team_tag(away)
    if ht: tags.append(ht)
    if at: tags.append(at)

    # 본문 키워드 기반 태그(최대 12개)
    for k, tg in _KEYWORD_TAGS:
        if k in body_nopick and tg not in tags:
            tags.append(tg)
        if len(tags) >= 12:
            break

    # dedupe keep order
    seen = set()
    tags2 = []
    for t in tags:
        if t and t not in seen:
            tags2.append(t); seen.add(t)
    hashtags = " ".join(tags2)

    parts = [intro]
    if core_one:
        parts.append(core_one)
    parts.append(outro)
    if pick_block:
        parts.append(pick_block)
    parts.append(hashtags)

    return "\n\n".join([p for p in parts if p]).strip()



def extract_final_pick_from_body(body: str) -> str:
    """body에서 [최종 픽] 섹션 중 '픽 라인'만 줄바꿈 그대로 추출해 반환.
    - [최종 픽] 이후 불릿(-,•,*) 또는 '승패/핸디/언오버' 라인만 연속으로 가져오고,
      그 다음 일반 문장(총평 등)이 나오면 거기서 중단한다.
    """
    if not body:
        return ""
    text = str(body)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # HTML 줄바꿈도 처리
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ")

    pats = [
        r"\[\s*최종\s*픽\s*\](.*?)(?:\n\s*[─\-]{5,}|\n\s*\[|\Z)",
        r"최종\s*픽\s*[:：]?\s*\n(.*?)(?:\n\s*[─\-]{5,}|\n\s*\[|\Z)",
    ]
    block = ""
    for pat in pats:
        m = re.search(pat, text, flags=re.S)
        if m:
            block = (m.group(1) or "").strip()
            if block:
                break
    if not block:
        return ""

    pick_lines = []
    started = False

    def is_pick_line(s: str) -> bool:
        s2 = s.strip()
        if not s2:
            return False
        if re.match(r"^[\-\•\*]+\s+", s2):
            return True
        if re.match(r"^(승패|핸디|언오버|오버|언더|결과|픽|추천)\s*[:：]", s2):
            return True
        return False

    for raw in block.split("\n"):
        s = raw.strip()
        if not s:
            if started:
                break
            continue

        if is_pick_line(s):
            started = True
            pick_lines.append(s)
        else:
            if started:
                break
            if re.match(r"^(승패\s*예상|핸디\s*예상|언오버\s*예상)\s*$", s):
                started = True
                pick_lines.append(s)
            else:
                continue

    if not pick_lines:
        return ""

    return "[최종 픽]\n" + "\n".join(pick_lines)

def ensure_export_header(ws) -> None:
    """export 시트 헤더가 7컬럼으로 맞지 않으면 강제로 맞춘다."""
    try:
        top = ws.row_values(1)
    except Exception:
        top = []
    if (not top) or top[: len(EXPORT_HEADER)] != EXPORT_HEADER:
        ws.update(range_name="A1", values=[EXPORT_HEADER])



def parse_maz_match_cards(soup: BeautifulSoup, target_date: str) -> list[dict]:
    """카드형 DOM(최근 maz 페이지)용 보조 파서.
    날짜 div 예: <div class="d-flex justify-center pa-0 mb-3 col">01-20 (화) 05:00</div>
    반환 dict 키: league, home, away, kickoff, link
    """
    results: list[dict] = []
    for d in soup.select("div.d-flex.justify-center.pa-0.mb-3.col"):
        kickoff_raw = clean_text(d.get_text(" ", strip=True))
        if _normalize_match_date(target_date, kickoff_raw) != target_date:
            continue

        # 링크 찾기(가까운 a 태그)
        a = d.find_parent("a")
        link = a.get("href") if a and a.get("href") else ""

        # 팀명 추정: 카드 텍스트에서 VS로 분리
        card = d
        for _ in range(6):
            if card is None:
                break
            # 좀 더 큰 컨테이너로 이동
            if hasattr(card, "parent"):
                card = card.parent
            else:
                break

        card_text = clean_text(card.get_text(" ", strip=True)) if card else ""
        home = away = ""
        if "VS" in card_text:
            parts = [p.strip() for p in card_text.split("VS") if p.strip()]
            if len(parts) >= 2:
                home = parts[0].split()[-1]
                away = parts[1].split()[0]

        results.append({
            "league": "",
            "home": home,
            "away": away,
            "kickoff": kickoff_raw,
            "link": link,
        })
    return results

import re

def _normalize_match_date(target_ymd: str, kickoff_raw: str) -> str:
    """kickoff_raw에서 날짜를 뽑아 target_ymd(YYYY-MM-DD)와 비교 가능한 형태로 만든다.
    지원 예:
      - '2026-01-19 04:45'
      - '2026.01.19 04:45'
      - '01-19(월) 04:45'
      - '01-19 04:45'
    """
    if not kickoff_raw:
        return ""
    s = str(kickoff_raw).strip()

    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    m = re.search(r"(20\d{2})\.(\d{2})\.(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    m = re.search(r"(\d{2})-(\d{2})", s)
    if m and re.match(r"20\d{2}-\d{2}-\d{2}", target_ymd or ""):
        year = target_ymd.split("-")[0]
        return f"{year}-{m.group(1)}-{m.group(2)}"

    return ""


import os
import json
import time
import asyncio
import re
import requests
import httpx

import traceback
# ───────────────── 네이버 카페 자동 글쓰기 (추후: '네이버 카페 자동 글쓰기') ─────────────────
# 공식 문서:
# - 네이버 로그인 토큰 발급/갱신: https://nid.naver.com/oauth2.0/token
# - 카페 글쓰기: https://openapi.naver.com/v1/cafe/{clubid}/menu/{menuid}/articles

NAVER_CLIENT_ID = (os.getenv("NAVER_CLIENT_ID") or "").strip()
NAVER_CLIENT_SECRET = (os.getenv("NAVER_CLIENT_SECRET") or "").strip()
NAVER_REFRESH_TOKEN = (os.getenv("NAVER_REFRESH_TOKEN") or "").strip()

# ───────────────── 뉴스 전용 네이버 계정(토큰 분리) ─────────────────
# 기존 분석글 업로드 토큰(NAVER_CLIENT_ID/SECRET/REFRESH_TOKEN)과 완전 분리
NAVER_NEWS_CLIENT_ID = (os.getenv("NAVER_NEWS_CLIENT_ID") or "").strip()
NAVER_NEWS_CLIENT_SECRET = (os.getenv("NAVER_NEWS_CLIENT_SECRET") or "").strip()
NAVER_NEWS_REFRESH_TOKEN = (os.getenv("NAVER_NEWS_REFRESH_TOKEN") or "").strip()

# 뉴스 게시판 menuId는 고정: 31
NAVER_CAFE_NEWS_MENU_ID = "31"

# 기본값(전 종목 공통 게시판). 종목별 분리는 NAVER_CAFE_MENU_MAP(JSON)로 설정 가능.
NAVER_CAFE_CLUBID = (os.getenv("NAVER_CAFE_CLUBID") or "").strip()
NAVER_CAFE_MENU_ID = (os.getenv("NAVER_CAFE_MENU_ID") or "").strip()
# 종목별 게시판(menuid). 비어있으면 NAVER_CAFE_MENU_MAP 또는 NAVER_CAFE_MENU_ID로 폴백.
NAVER_CAFE_MENU_ID_SOCCER = (os.getenv("NAVER_CAFE_MENU_ID_SOCCER") or "").strip()
NAVER_CAFE_MENU_ID_BASEBALL = (os.getenv("NAVER_CAFE_MENU_ID_BASEBALL") or "").strip()
NAVER_CAFE_MENU_ID_BASKETBALL = (os.getenv("NAVER_CAFE_MENU_ID_BASKETBALL") or "").strip()
NAVER_CAFE_MENU_ID_VOLLEYBALL = (os.getenv("NAVER_CAFE_MENU_ID_VOLLEYBALL") or "").strip()

# 심층 분석 게시판(menuid) - 기본값(요청 반영): 축구45/농구46/야구47/배구48
# 필요하면 환경변수로 덮어쓸 수 있음.
NAVER_CAFE_MENU_ID_DEEP_SOCCER = (os.getenv("NAVER_CAFE_MENU_ID_DEEP_SOCCER") or "45").strip()
NAVER_CAFE_MENU_ID_DEEP_BASKETBALL = (os.getenv("NAVER_CAFE_MENU_ID_DEEP_BASKETBALL") or "46").strip()
NAVER_CAFE_MENU_ID_DEEP_BASEBALL = (os.getenv("NAVER_CAFE_MENU_ID_DEEP_BASEBALL") or "47").strip()
NAVER_CAFE_MENU_ID_DEEP_VOLLEYBALL = (os.getenv("NAVER_CAFE_MENU_ID_DEEP_VOLLEYBALL") or "48").strip()

# 예: {"soccer":"10","baseball":"20","basketball":"30","volleyball":"40"}
NAVER_CAFE_MENU_MAP_RAW = (os.getenv("NAVER_CAFE_MENU_MAP") or "").strip()

# 예: {"soccer":"45","baseball":"47","basketball":"46","volleyball":"48"}
NAVER_CAFE_MENU_MAP_DEEP_RAW = (os.getenv("NAVER_CAFE_MENU_MAP_DEEP") or "").strip()

CAFE_LOG_SHEET_NAME = (os.getenv("CAFE_LOG_SHEET_NAME") or "cafe_log").strip()
CAFE_LOG_HEADER = ["src_id", "day", "sport", "clubid", "menuid", "articleId", "postedAt", "status", "title", "url", "deep_url"]

# ───────────────── news_cafe_queue / news_cafe_log ─────────────────
NEWS_CAFE_QUEUE_SHEET_NAME = (os.getenv("NEWS_CAFE_QUEUE_SHEET_NAME") or "news_cafe_queue").strip()
NEWS_CAFE_QUEUE_HEADER = ["createdAt", "sport", "title", "url", "status", "postedAt", "error"]

NEWS_CAFE_LOG_SHEET_NAME = (os.getenv("NEWS_CAFE_LOG_SHEET_NAME") or "news_cafe_log").strip()
NEWS_CAFE_LOG_HEADER = ["url", "title", "postedAt", "status", "error"]

# 상태값: NEW, POSTED, FAIL

_naver_access_token_cache = {"token": "", "expires_at": 0}

_naver_news_access_token_cache = {"token": "", "expires_at": 0}

def _naver_menu_id_for_sport(sport: str) -> str:
    sport_key = (sport or "").strip().lower()

    # 1) 종목별 환경변수 우선
    if sport_key == "soccer" and NAVER_CAFE_MENU_ID_SOCCER:
        return NAVER_CAFE_MENU_ID_SOCCER
    if sport_key == "baseball" and NAVER_CAFE_MENU_ID_BASEBALL:
        return NAVER_CAFE_MENU_ID_BASEBALL
    if sport_key == "basketball" and NAVER_CAFE_MENU_ID_BASKETBALL:
        return NAVER_CAFE_MENU_ID_BASKETBALL
    if sport_key == "volleyball" and NAVER_CAFE_MENU_ID_VOLLEYBALL:
        return NAVER_CAFE_MENU_ID_VOLLEYBALL

    # 2) JSON 맵 폴백: {"soccer":"10", ...}
    if NAVER_CAFE_MENU_MAP_RAW:
        try:
            mp = json.loads(NAVER_CAFE_MENU_MAP_RAW)
            v = mp.get(sport_key, "")
            if v:
                return str(v).strip()
        except Exception:
            pass

    # 3) 단일 메뉴 폴백
    return NAVER_CAFE_MENU_ID


def _naver_menu_id_for_sport_deep(sport: str) -> str:
    """심층 분석 게시판(menuid) 반환."""
    sport_key = (sport or "").strip().lower()

    # 1) JSON 맵(환경변수) 우선: NAVER_CAFE_MENU_MAP_DEEP = {"soccer":"45", ...}
    if NAVER_CAFE_MENU_MAP_DEEP_RAW:
        try:
            mp = json.loads(NAVER_CAFE_MENU_MAP_DEEP_RAW)
            v = mp.get(sport_key, "")
            if v:
                return str(v).strip()
        except Exception:
            pass

    # 2) 종목별 환경변수 폴백
    if sport_key == "soccer":
        return NAVER_CAFE_MENU_ID_DEEP_SOCCER
    if sport_key == "basketball":
        return NAVER_CAFE_MENU_ID_DEEP_BASKETBALL
    if sport_key == "baseball":
        return NAVER_CAFE_MENU_ID_DEEP_BASEBALL
    if sport_key == "volleyball":
        return NAVER_CAFE_MENU_ID_DEEP_VOLLEYBALL
    # 알 수 없는 값이면(혹시 모를 확장) 단일 메뉴 폴백
    return NAVER_CAFE_MENU_ID

def _naver_have_config() -> bool:
    return bool(NAVER_CLIENT_ID and NAVER_CLIENT_SECRET and NAVER_REFRESH_TOKEN and NAVER_CAFE_CLUBID)

def _naver_refresh_access_token() -> str:
    """refresh_token으로 access_token 갱신(캐시 사용)."""
    now_ts = int(time.time())
    # 60초 여유
    if _naver_access_token_cache["token"] and _naver_access_token_cache["expires_at"] - 60 > now_ts:
        return _naver_access_token_cache["token"]

    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET and NAVER_REFRESH_TOKEN):
        return ""

    url = "https://nid.naver.com/oauth2.0/token"
    params = {
        "grant_type": "refresh_token",
        "client_id": NAVER_CLIENT_ID,
        "client_secret": NAVER_CLIENT_SECRET,
        "refresh_token": NAVER_REFRESH_TOKEN,
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        if not (200 <= r.status_code < 300):
            print(f"[NAVER] token refresh failed status={r.status_code} body={r.text[:500]}")
            return ""
        data = r.json()
        token = (data.get("access_token") or "").strip()
        expires_in = int(str(data.get("expires_in") or "3600"))
        if token:
            _naver_access_token_cache["token"] = token
            _naver_access_token_cache["expires_at"] = now_ts + max(60, expires_in)
        return token
    except Exception as e:
        print(f"[NAVER] token refresh exception: {e}")
        return ""


def _naver_news_have_config() -> bool:
    return bool(NAVER_NEWS_CLIENT_ID and NAVER_NEWS_CLIENT_SECRET and NAVER_NEWS_REFRESH_TOKEN and NAVER_CAFE_CLUBID)

def _naver_news_refresh_access_token() -> str:
    """뉴스 전용 refresh_token으로 access_token 갱신(캐시 사용)."""
    now_ts = int(time.time())
    if _naver_news_access_token_cache["token"] and _naver_news_access_token_cache["expires_at"] - 60 > now_ts:
        return _naver_news_access_token_cache["token"]

    if not (NAVER_NEWS_CLIENT_ID and NAVER_NEWS_CLIENT_SECRET and NAVER_NEWS_REFRESH_TOKEN):
        return ""

    url = "https://nid.naver.com/oauth2.0/token"
    params = {
        "grant_type": "refresh_token",
        "client_id": NAVER_NEWS_CLIENT_ID,
        "client_secret": NAVER_NEWS_CLIENT_SECRET,
        "refresh_token": NAVER_NEWS_REFRESH_TOKEN,
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        if not (200 <= r.status_code < 300):
            print(f"[NAVER][NEWS] token refresh failed status={r.status_code} body={r.text[:500]}")
            return ""
        data = r.json()
        token = (data.get("access_token") or "").strip()
        expires_in = int(str(data.get("expires_in") or "3600"))
        if token:
            _naver_news_access_token_cache["token"] = token
            _naver_news_access_token_cache["expires_at"] = now_ts + max(60, expires_in)
        return token
    except Exception as e:
        print(f"[NAVER][NEWS] token refresh exception: {e}")
        return ""

def _naver_clean_text(s: str) -> str:
    s = (s or "")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # 네이버 카페 에디터가 싫어하는 제어문자 제거
    s = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", s)
    return s.strip()

def _naver_quote_double(s: str) -> str:
    """네이버 카페 글쓰기 API에서 한글 깨짐 방지를 위한 이중 URL 인코딩."""
    s = _naver_clean_text(s)
    first = quote_plus(s, safe="", encoding="utf-8", errors="strict")
    second = quote_plus(first, safe="", encoding="ms949", errors="ignore")
    return second

def _naver_quote_once(s: str) -> str:
    s = _naver_clean_text(s)
    return quote_plus(s, safe="", encoding="utf-8", errors="strict")

def _naver_cafe_post(subject: str, content: str, clubid: str, menuid: str) -> tuple[bool, str]:
    """네이버 카페에 글쓰기. (success, articleId_or_error)

    - application/x-www-form-urlencoded 로 전송
    - subject/content 를 URL 인코딩(기본: UTF-8→MS949 이중 인코딩)해서 한글 깨짐 방지
    """
    token = _naver_refresh_access_token()
    if not token:
        return False, "NO_ACCESS_TOKEN"
    if not clubid or not menuid:
        return False, "NO_CLUBID_OR_MENUID"

    subject_raw = (subject or "").strip() or "스포츠 분석"
    if len(subject_raw) > 80:
        subject_raw = subject_raw[:80]

    content_raw = content or ""

    use_double = str(os.getenv("NAVER_CAFE_DOUBLE_ENCODE", "1")).strip() not in ("0", "false", "False", "no", "NO")

    enc_subject = _naver_quote_double(subject_raw) if use_double else _naver_quote_once(subject_raw)
    enc_content  = _naver_quote_double(content_raw) if use_double else _naver_quote_once(content_raw)

    body = f"subject={enc_subject}&content={enc_content}"

    url = f"https://openapi.naver.com/v1/cafe/{clubid}/menu/{menuid}/articles"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }

    try:
        resp = requests.post(url, headers=headers, data=body.encode("utf-8"), timeout=30)

        if resp.status_code != 200:
            txt = (resp.text or "")[:800]
            try:
                j = resp.json()
                code = j.get("message", {}).get("error", {}).get("code")
                msg = j.get("message", {}).get("error", {}).get("msg")
                if code or msg:
                    return False, f"HTTP_{resp.status_code}:{code}:{msg}"
            except Exception:
                pass
            return False, f"HTTP_{resp.status_code}:{txt}"

        article_id = ""
        try:
            j = resp.json()
            if isinstance(j, dict):
                article_id = (
                    j.get("message", {}).get("result", {}).get("articleId")
                    or j.get("result", {}).get("articleId")
                    or ""
                )
        except Exception:
            pass

        return True, (str(article_id) if article_id else "OK")

    except Exception as e:
        return False, f"EXC:{e}"

def _naver_news_cafe_post(subject: str, content: str, clubid: str, menuid: str) -> tuple[bool, str]:
    """뉴스 전용 계정으로 네이버 카페에 글쓰기. (success, articleId_or_error)

    - application/x-www-form-urlencoded 로 전송
    - subject/content 를 URL 인코딩(기본: UTF-8→MS949 이중 인코딩)해서 한글 깨짐 방지
    """
    token = _naver_news_refresh_access_token()
    if not token:
        return False, "NO_ACCESS_TOKEN_NEWS"
    if not clubid or not menuid:
        return False, "NO_CLUBID_OR_MENUID"

    subject_raw = (subject or "").strip() or "스포츠 뉴스"
    if len(subject_raw) > 80:
        subject_raw = subject_raw[:80]

    content_raw = content or ""

    use_double = str(os.getenv("NAVER_CAFE_DOUBLE_ENCODE", "1")).strip() not in ("0", "false", "False", "no", "NO")

    enc_subject = _naver_quote_double(subject_raw) if use_double else _naver_quote_once(subject_raw)
    enc_content  = _naver_quote_double(content_raw) if use_double else _naver_quote_once(content_raw)

    body = f"subject={enc_subject}&content={enc_content}"

    url = f"https://openapi.naver.com/v1/cafe/{clubid}/menu/{menuid}/articles"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }

    try:
        resp = requests.post(url, headers=headers, data=body.encode("utf-8"), timeout=30)

        if resp.status_code != 200:
            txt = (resp.text or "")[:800]
            try:
                j = resp.json()
                code = j.get("message", {}).get("error", {}).get("code")
                msg = j.get("message", {}).get("error", {}).get("msg")
                if code or msg:
                    return False, f"HTTP_{resp.status_code}:{code}:{msg}"
            except Exception:
                pass
            return False, f"HTTP_{resp.status_code}:{txt}"

        article_id = ""
        try:
            j = resp.json()
            if isinstance(j, dict):
                article_id = (
                    j.get("message", {}).get("result", {}).get("articleId")
                    or j.get("result", {}).get("articleId")
                    or ""
                )
        except Exception:
            pass

        return True, (str(article_id) if article_id else "OK")

    except Exception as e:
        return False, f"EXC:{e}"


def _naver_news_cafe_post_multipart(
    subject: str,
    content: str,
    clubid: str,
    menuid: str,
    *,
    image_bytes: bytes,
    filename: str = "image.jpg",
    mime_type: str = "image/jpeg",
) -> tuple[bool, str]:
    """뉴스 전용 계정으로 글쓰기 + 이미지 1장 첨부(multipart).

    ✅ 목표
    - 제목/본문 한글이 %ED%.. 형태(퍼센트 문자열)로 올라가거나, 깨지는 문제 방지
    - 이미지 업로드 실패 시에도 글은 이미지 없이 업로드(fallback)되도록(상위 로직에서 처리)

    ✅ 구현 포인트(실전 안정화)
    1) 네이버 개발자센터 공식 예제 방식대로 subject/content 값을 UTF-8 URL 인코딩(1회)해서 전송(ENCODED_UTF8)
       - 일부 환경에서 RAW가 더 잘 먹는 케이스가 있어 RAW_UTF8도 추가로 시도(환경변수로 순서 변경 가능)
    2) 파일 파트 필드명: 공식 예제에 따라 'image'를 우선 사용, 그 외 'image[0]', '0'도 폴백 시도
    3) 403 + code=999(연속등록/일시적 제한) 계열은 짧게 백오프 후 재시도
    4) 이미지 용량/포맷 이슈(webp/avif/과대용량 등)로 multipart가 실패할 수 있어
       - 1차: 원본 업로드 시도
       - 2차: (필요 시) 비율 유지 리사이즈 + JPEG 재인코딩(자르지 않음) 후 재시도
    """
    token = _naver_news_refresh_access_token()
    if not token:
        return False, "NO_ACCESS_TOKEN_NEWS"
    if not clubid or not menuid:
        return False, "NO_CLUBID_OR_MENUID"
    if not image_bytes:
        return False, "NO_IMAGE_BYTES"

    # ✅ 제목은 항상 사람이 읽을 수 있는 문자열로 정리
    subject_raw = _naver_clean_text(_safe_url_decode(subject) or "스포츠 뉴스").strip()
    if len(subject_raw) > 80:
        subject_raw = subject_raw[:80]

    # ✅ content는 HTML 포함 문자열이므로 제어문자만 제거(태그 유지)
    content_raw = _naver_clean_text(content or "")

    url = f"https://openapi.naver.com/v1/cafe/{clubid}/menu/{menuid}/articles"

    # ✅ 헤더(Authorization은 필수)
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0",
    }
    # (옵션) 일부 샘플(C#)에서 Client ID/Secret 헤더도 함께 보내는 케이스가 있어 같이 넣어 둠(무해)
    if NAVER_NEWS_CLIENT_ID:
        headers["X-Naver-Client-Id"] = NAVER_NEWS_CLIENT_ID
    if NAVER_NEWS_CLIENT_SECRET:
        headers["X-Naver-Client-Secret"] = NAVER_NEWS_CLIENT_SECRET

    from urllib.parse import quote

    # ✅ 기본: UTF-8 URL 인코딩 1회 (공식 예제 방식)
    subject_enc = quote(subject_raw, safe="", encoding="utf-8", errors="strict")
    content_enc = quote(content_raw, safe="", encoding="utf-8", errors="strict")

    data_encoded = {"subject": subject_enc, "content": content_enc}
    data_raw = {"subject": subject_raw, "content": content_raw}

    # 환경에 따라 raw가 더 잘 먹는 케이스가 있어 순서 토글 가능
    prefer_raw = (os.getenv("NAVER_NEWS_MULTIPART_PREFER_RAW") or "0").strip().lower() in ("1", "true", "yes", "y", "on")
    data_variants: list[tuple[str, dict]] = [("ENCODED_UTF8", data_encoded), ("RAW_UTF8", data_raw)]
    if prefer_raw:
        data_variants = [("RAW_UTF8", data_raw), ("ENCODED_UTF8", data_encoded)]

    # ✅ 파일 파트 헤더(공식 예제에 Expires: 0 사용 사례가 있어 안전하게 포함)
    part_headers = {"Expires": "0"}

    # rate limit 백오프
    backoff_sec = float(os.getenv("NAVER_NEWS_MULTIPART_BACKOFF_SEC", "15"))
    max_retries = int(os.getenv("NAVER_NEWS_MULTIPART_RETRIES", "2"))
    import time as _time

    def _parse_err(resp: requests.Response) -> tuple[str, str, str]:
        """(status, code, msg)"""
        status = str(resp.status_code)
        code = ""
        msg = ""
        try:
            j = resp.json()
            code = (j.get("message", {}).get("error", {}).get("code") or "").strip()
            msg = (j.get("message", {}).get("error", {}).get("msg") or "").strip()
        except Exception:
            pass
        if not msg:
            msg = ((resp.text or "")[:300]).strip()
        return status, code, msg

    def _is_rate_limited(status: str, code: str, msg: str) -> bool:
        # 403 + code=999가 가장 흔한 케이스(연속등록/일시적 제한)
        if status == "403" and (code == "999" or "999" in code):
            return True
        # 메시지 기반 폴백
        m = (msg or "")
        if ("연속" in m and "등록" in m) or ("잠시" in m and "후" in m) or ("오류가 발생" in m):
            return True
        return False

    def _shrink_image_for_naver(data: bytes, mime: str) -> tuple[bytes, str, str]:
        """비율 유지(자르지 않음)로 리사이즈 + JPEG 재인코딩하여 용량을 줄인다.
        실패 시 원본 그대로 반환.
        """
        if not data:
            return data, filename, mime_type

        # 이미 충분히 작고, 포맷도 안전하면 그대로
        max_bytes = int(os.getenv("NAVER_NEWS_IMAGE_MAX_BYTES", "1800000"))  # 1.8MB 기본
        if (len(data) <= max_bytes) and (mime in ("image/jpeg", "image/png")):
            return data, filename, mime

        try:
            from PIL import Image  # type: ignore
            from io import BytesIO

            im = Image.open(BytesIO(data))
            im.load()

            # 투명도 처리(흰 배경)
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                im = im.convert("RGBA")
                bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg.convert("RGB")
            else:
                im = im.convert("RGB")

            # 최대 변 길이 제한(자르지 않고 축소만)
            max_dim = int(os.getenv("NAVER_NEWS_IMAGE_MAX_DIM", "1600"))  # 기본 1600px
            w, h = im.size
            mx = max(w, h)
            if mx > max_dim and mx > 0:
                scale = max_dim / float(mx)
                nw = max(1, int(round(w * scale)))
                nh = max(1, int(round(h * scale)))
                im = im.resize((nw, nh), Image.LANCZOS)

            # 품질을 단계적으로 낮추며 max_bytes 이하로 맞춘다
            qualities = [92, 88, 85, 82, 78, 74, 70, 65]
            best = b""
            for q in qualities:
                out = BytesIO()
                im.save(out, format="JPEG", quality=q, optimize=True, progressive=True)
                b = out.getvalue()
                best = b
                if len(b) <= max_bytes:
                    break

            # 너무 큰 경우 max_dim을 더 줄여 1회 더 시도
            if best and len(best) > max_bytes:
                max_dim2 = int(os.getenv("NAVER_NEWS_IMAGE_MAX_DIM_FALLBACK", "1280"))
                if max_dim2 < max_dim and max(im.size) > max_dim2:
                    w2, h2 = im.size
                    mx2 = max(w2, h2)
                    if mx2 > 0:
                        scale2 = max_dim2 / float(mx2)
                        nw2 = max(1, int(round(w2 * scale2)))
                        nh2 = max(1, int(round(h2 * scale2)))
                        im2 = im.resize((nw2, nh2), Image.LANCZOS)
                        out2 = BytesIO()
                        im2.save(out2, format="JPEG", quality=82, optimize=True, progressive=True)
                        best2 = out2.getvalue()
                        if best2:
                            best = best2

            if best:
                return best, "news_image.jpg", "image/jpeg"
        except Exception:
            pass

        return data, filename, mime

    def _attempt_post(img_b: bytes, img_fn: str, img_mime: str) -> tuple[bool, str]:
        last_err = ""
        # field name 우선순위: 공식 예제 기반으로 image → image[0] → 0
        for variant_name, data in data_variants:
            for field_name in ("image", "image[0]", "0"):
                files = [(field_name, (img_fn, img_b, img_mime, part_headers))]

                # 같은 조합으로 rate-limit 재시도
                for attempt in range(max_retries + 1):
                    try:
                        resp = requests.post(url, headers=headers, data=data, files=files, timeout=70)

                        if resp.status_code != 200:
                            st, code, msg = _parse_err(resp)
                            last_err = f"{variant_name}:{field_name}:HTTP_{st}:{code}:{msg}"

                            if _is_rate_limited(st, code, msg) and attempt < max_retries:
                                sleep_s = backoff_sec * (attempt + 1)
                                print(f"[NEWS_IMAGE] rate-limit 감지({st}/{code}). {sleep_s:.0f}s 대기 후 재시도...")
                                _time.sleep(sleep_s)
                                continue

                            break  # 다음 field/variant 시도
                        # success
                        article_id = ""
                        try:
                            j = resp.json()
                            if isinstance(j, dict):
                                article_id = (
                                    j.get("message", {}).get("result", {}).get("articleId")
                                    or j.get("result", {}).get("articleId")
                                    or ""
                                )
                        except Exception:
                            pass

                        return True, (str(article_id) if article_id else "OK")

                    except Exception as e:
                        last_err = f"{variant_name}:{field_name}:EXC:{e}"
                        break

        return False, (last_err or "UPLOAD_FAIL")

    # 1) 원본 업로드 1차
    ok, info = _attempt_post(image_bytes, filename or "image.jpg", mime_type or "image/jpeg")
    if ok:
        return True, info

    # 2) (필요 시) 축소/재인코딩 버전으로 2차(원본을 자르지 않고 '비율 유지 축소'만)
    small_b, small_fn, small_mime = _shrink_image_for_naver(image_bytes, mime_type or "")
    if small_b and (small_b != image_bytes):
        ok2, info2 = _attempt_post(small_b, small_fn, small_mime)
        if ok2:
            return True, info2
        # 2차도 실패하면 2차 에러를 더 우선 노출
        return False, info2

    return False, info


def get_cafe_log_ws():
    """cafe_log 워크시트 반환(없으면 생성 + 헤더 세팅)."""
    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not (client_gs and spreadsheet_id):
        return None
    try:
        sh = client_gs.open_by_key(spreadsheet_id)
        ws = _get_ws_by_name(sh, CAFE_LOG_SHEET_NAME)
        if not ws:
            ws = sh.add_worksheet(title=CAFE_LOG_SHEET_NAME, rows=2000, cols=12)
        ws.resize(cols=max(12, len(CAFE_LOG_HEADER)))
        _ensure_header(ws, CAFE_LOG_HEADER)
        return ws
    except Exception as e:
        print(f"[GSHEET] cafe_log ws error: {e}")
        return None


def get_news_cafe_queue_ws():
    """news_cafe_queue 워크시트 반환(없으면 생성 + 헤더 세팅)."""
    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not (client_gs and spreadsheet_id):
        return None
    try:
        sh = client_gs.open_by_key(spreadsheet_id)
        ws = _get_ws_by_name(sh, NEWS_CAFE_QUEUE_SHEET_NAME)
        if not ws:
            ws = sh.add_worksheet(title=NEWS_CAFE_QUEUE_SHEET_NAME, rows=5000, cols=20)
        ws.resize(cols=max(12, len(NEWS_CAFE_QUEUE_HEADER)))
        _ensure_header(ws, NEWS_CAFE_QUEUE_HEADER)
        return ws
    except Exception as e:
        print(f"[GSHEET] news_cafe_queue ws error: {e}")
        return None


def get_news_cafe_log_ws():
    """news_cafe_log 워크시트 반환(없으면 생성 + 헤더 세팅)."""
    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not (client_gs and spreadsheet_id):
        return None
    try:
        sh = client_gs.open_by_key(spreadsheet_id)
        ws = _get_ws_by_name(sh, NEWS_CAFE_LOG_SHEET_NAME)
        if not ws:
            ws = sh.add_worksheet(title=NEWS_CAFE_LOG_SHEET_NAME, rows=5000, cols=20)
        ws.resize(cols=max(12, len(NEWS_CAFE_LOG_HEADER)))
        _ensure_header(ws, NEWS_CAFE_LOG_HEADER)
        return ws
    except Exception as e:
        print(f"[GSHEET] news_cafe_log ws error: {e}")
        return None


def _normalize_news_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    # 상대경로면 다음스포츠로 보정
    if u.startswith("/"):
        u = urljoin("https://sports.daum.net", u)
    # fragment 제거
    u = u.split("#", 1)[0].strip()
    return u


def _load_news_queue_urls(ws_queue) -> set[str]:
    """news_cafe_queue에 이미 존재하는 normalized_url set."""
    existing: set[str] = set()
    try:
        vals = ws_queue.get_all_values()
        if not vals or len(vals) <= 1:
            return existing
        header = vals[0]
        try:
            idx_url = header.index("url")
        except ValueError:
            idx_url = 3  # 기본 4번째 컬럼 가정
        for r in vals[1:]:
            if len(r) <= idx_url:
                continue
            u = _normalize_news_url(r[idx_url])
            if u:
                existing.add(u)
    except Exception:
        pass
    return existing


def enqueue_news_to_cafe_queue(*, sport_label: str, articles: list[dict]) -> int:
    """다음뉴스 크롤링 시점에 원문 정보를 news_cafe_queue에 동시 적재한다.

    - 기존 news 탭 저장/텔레그램 게시 흐름은 건드리지 않음(추가만).
    - url 정규화 후, queue에 이미 같은 url이 있으면 스킵.
    """
    ws_q = get_news_cafe_queue_ws()
    if not ws_q:
        return 0

    existing = _load_news_queue_urls(ws_q)

    now_iso = now_kst().isoformat()
    rows: list[list[str]] = []
    for art in (articles or []):
        try:
            title = (art.get("title") or "").strip()
            url = _normalize_news_url(art.get("link") or art.get("url") or "")
            if not url:
                continue
            if url in existing:
                continue
            rows.append([now_iso, sport_label, title, url, "NEW", "", ""])
            existing.add(url)
        except Exception:
            continue

    if not rows:
        return 0

    try:
        ws_q.append_rows(rows, value_input_option="RAW", table_range="A1")
        return len(rows)
    except Exception as e:
        print(f"[GSHEET] news_cafe_queue append_rows error: {e}")
        return 0


def _load_news_cafe_posted_urls(ws_log) -> set[str]:
    """news_cafe_log에서 OK/POSTED 처리된 url만 로드."""
    posted: set[str] = set()
    try:
        vals = ws_log.get_all_values()
        if not vals or len(vals) <= 1:
            return posted
        header = vals[0]
        try:
            idx_url = header.index("url")
        except ValueError:
            idx_url = 0
        try:
            idx_status = header.index("status")
        except ValueError:
            idx_status = 3
        for r in vals[1:]:
            u = _normalize_news_url(r[idx_url] if len(r) > idx_url else "")
            st = (r[idx_status] if len(r) > idx_status else "").strip().upper()
            if not u:
                continue
            if st in ("OK", "POSTED"):
                posted.add(u)
    except Exception:
        pass
    return posted

def _load_posted_keys(ws_log) -> set:
    """cafe_log에서 (src_id, menuid) 단위로 '성공(OK)'한 게시글만 로드.
    - 심플/심층 게시판을 분리해서 중복 업로드를 제어하기 위해 menuid까지 같이 본다.
    - 실패 로그는 재시도 가능하도록 제외한다.
    """
    posted = set()
    try:
        vals = ws_log.get_all_values()
        if not vals or len(vals) <= 1:
            return posted
        header = vals[0]

        def _idx(name: str, default: int) -> int:
            try:
                return header.index(name)
            except ValueError:
                return default

        idx_src = _idx("src_id", 0)
        idx_menu = _idx("menuid", 4)
        idx_status = _idx("status", 7)

        for r in vals[1:]:
            sid = r[idx_src].strip() if len(r) > idx_src else ""
            mid = r[idx_menu].strip() if len(r) > idx_menu else ""
            st = r[idx_status].strip() if len(r) > idx_status else ""
            if not sid or not mid:
                continue
            if st != "OK":
                continue
            posted.add(f"{sid}|{mid}")
    except Exception:
        pass
    return posted

# 하위 호환: 예전 함수명 (src_id만)도 남겨둠
def _load_posted_src_ids(ws_log) -> set:
    keys = _load_posted_keys(ws_log)
    return {k.split("|", 1)[0] for k in keys if "|" in k}


def _cafe_sport_match(sport_value: str, sport_filter: str) -> bool:
    """export 시트의 sport 값이 명령어 sport_filter(축구/야구/농구/배구)와 매칭되는지 판정."""
    sv = (sport_value or "").strip().lower()
    sf = (sport_filter or "").strip().lower()
    if not sf:
        return True
    # 같은 값이면 바로 통과
    if sv == sf:
        return True

    # 명령어 필터별 허용 sport 값(시트에서 쓰는 리그명까지 포함)
    aliases = {
        "soccer": {
            "soccer", "football",
            "축구", "해외축구", "국내축구", "해축", "국축", "k리그", "k리그1", "k리그2", "k-league", "kleague", "j리그", "j1", "j2", "jleague", "j-league", "j리그1", "j리그2", "epl", "ucl", "uefa", "챔피언스리그", "유로파", "컨퍼런스",
        },
        "baseball": {
            "baseball", "야구",
            "kbo", "mlb", "npb", "프로야구", "해외야구", "국내야구",
        },
        "basketball": {
            "basketball", "농구",
            "nba", "kbl", "wkbl", "wnba",
        },
        "volleyball": {
            "volleyball", "배구",
            "v리그", "vleague", "kovo", "프로배구", "남자배구", "여자배구", "vnl", "avc",
        },
    }
    # sport_filter가 한글로 들어오는 경우도 대비
    sf_norm = {
        "축구": "soccer",
        "야구": "baseball",
        "농구": "basketball",
        "배구": "volleyball",
    }.get(sf, sf)

    allowed = aliases.get(sf_norm, {sf_norm})
    return sv in allowed


async def _cafe_parse_which_arg(context: ContextTypes.DEFAULT_TYPE) -> str:
    which = "today"
    args = getattr(context, "args", None) or []
    if args:
        a0 = (args[0] or "").strip().lower()
        if a0 in ("tomorrow", "tmr", "t", "내일"):
            which = "tomorrow"
    return which

async def cafe_soccer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    which = await _cafe_parse_which_arg(context)
    return await cafe_post_from_export(update, context, which, sport_filter="soccer")

async def cafe_baseball(update: Update, context: ContextTypes.DEFAULT_TYPE):
    which = await _cafe_parse_which_arg(context)
    return await cafe_post_from_export(update, context, which, sport_filter="baseball")

async def cafe_basketball(update: Update, context: ContextTypes.DEFAULT_TYPE):
    which = await _cafe_parse_which_arg(context)
    return await cafe_post_from_export(update, context, which, sport_filter="basketball")

async def cafe_volleyball(update: Update, context: ContextTypes.DEFAULT_TYPE):
    which = await _cafe_parse_which_arg(context)
    return await cafe_post_from_export(update, context, which, sport_filter="volleyball")

# 심층 분석 게시판 업로드: /cafe_soccer_deep [tomorrow]
async def cafe_soccer_deep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    which = await _cafe_parse_which_arg(context)
    return await cafe_post_from_export(update, context, which, sport_filter="soccer", mode="deep")

async def cafe_baseball_deep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    which = await _cafe_parse_which_arg(context)
    return await cafe_post_from_export(update, context, which, sport_filter="baseball", mode="deep")

async def cafe_basketball_deep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    which = await _cafe_parse_which_arg(context)
    return await cafe_post_from_export(update, context, which, sport_filter="basketball", mode="deep")

async def cafe_volleyball_deep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    which = await _cafe_parse_which_arg(context)
    return await cafe_post_from_export(update, context, which, sport_filter="volleyball", mode="deep")


async def cafe_post_from_export(update: Update, context: ContextTypes.DEFAULT_TYPE, which: str, sport_filter: str = "", mode: str = "simple"):
    """export_today/export_tomorrow 내용을 네이버 카페에 업로드.

    - mode="simple": export_* 시트의 G열(simple) 업로드(기존 심플 분석 게시판)
    - mode="deep":   export_* 시트의 E열(body) 업로드(심층 분석 게시판)
    - 본문은 <center> + <br> 조합으로 업로드(카페 필터링으로 style 속성이 제거되는 경우 방지)
    - 한글 깨짐 방지: _naver_cafe_post() 내부에서 URL 인코딩(옵션: 이중 인코딩) 처리
    - 연속 등록 제한 대응: 기본 딜레이 + 403/연속등록/999류 백오프 후 재시도
    """
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    mode = (mode or "simple").strip().lower()
    if mode not in ("simple", "deep"):
        mode = "simple"

    if not _naver_have_config():
        await update.message.reply_text(
            "네이버 카페 자동 글쓰기 설정이 비어있어. 환경변수에 아래를 넣어줘:\n"
            "NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, NAVER_REFRESH_TOKEN, NAVER_CAFE_CLUBID, "
            "NAVER_CAFE_MENU_ID_SOCCER/BASEBALL/BASKETBALL/VOLLEYBALL (또는 NAVER_CAFE_MENU_MAP/NAVER_CAFE_MENU_ID 폴백)\n"
            "※ 심층 게시판은 기본값(45/46/47/48)을 쓰고, 필요하면 NAVER_CAFE_MENU_ID_DEEP_* 로 덮어쓸 수 있어."
        )
        return

    sheet_name = EXPORT_TODAY_SHEET_NAME if which == "today" else EXPORT_TOMORROW_SHEET_NAME
    ws = get_export_ws(sheet_name)
    ws_log = get_cafe_log_ws()
    if not ws:
        await update.message.reply_text(f"{sheet_name} 시트를 못 찾았어.")
        return
    if not ws_log:
        await update.message.reply_text("cafe_log 시트를 준비 못 했어(SPREADSHEET_ID 확인).")
        return

    # export 헤더 확장(구버전이면 cafe_url 컬럼 등 추가)
    try:
        ensure_export_schema(ws, EXPORT_HEADER)
    except Exception:
        pass

    vals = ws.get_all_values()
    if not vals or len(vals) <= 1:
        await update.message.reply_text(f"{sheet_name}에 업로드할 데이터가 없어.")
        return

    header = vals[0]

    def col(name: str, default: int = -1) -> int:
        try:
            return header.index(name)
        except ValueError:
            return default

    i_day = col("day", 0)
    i_sport = col("sport", 1)
    i_src = col("src_id", 2)
    i_title = col("title", 3)
    i_body = col("body", 4)
    i_created = col("createdAt", 5)
    i_simple = col("simple", 6)
    i_cafe_title = col("cafe_title", -1)
    i_cafe_url = col("cafe_url", -1)
    i_cafe_url_deep = col("cafe_url_deep", -1)


    posted_keys = _load_posted_keys(ws_log)

    def _infer_sport_key(sportv: str) -> str:
        for k in ("soccer", "baseball", "basketball", "volleyball"):
            if _cafe_sport_match(sportv, k):
                return k
        return ""

    to_post = []
    for row_idx, r in enumerate(vals[1:], start=2):
        sid = (r[i_src].strip() if len(r) > i_src else "")
        if not sid:
            continue

        dayv = r[i_day].strip() if len(r) > i_day else ""
        sportv = r[i_sport].strip() if len(r) > i_sport else ""
        if sport_filter and (not _cafe_sport_match(sportv, sport_filter)):
            continue

        titlev = r[i_title].strip() if len(r) > i_title else ""
        createdv = r[i_created].strip() if len(r) > i_created else ""

        # menuid 결정 (심플/심층 게시판 분리)
        sport_key = (sport_filter or _infer_sport_key(sportv)).strip().lower()
        if mode == "deep":
            menuid = _naver_menu_id_for_sport_deep(sport_key)
        else:
            menuid = _naver_menu_id_for_sport(sport_key)

        # 이미 같은 게시판(menuid)에 올린 글이면 제외
        if menuid and f"{sid}|{menuid}" in posted_keys:
            continue

        # mode에 따라 업로드할 본문 선택
        if mode == "deep":
            contentv = r[i_body] if len(r) > i_body else ""
        else:
            contentv = r[i_simple] if len(r) > i_simple else ""


        # ✅ 마지막 안전 처리: 브랜드/구분자 제거(시트에 남아있어도 업로드 전에 정리)
        contentv = _postprocess_site_body_text(contentv)
        to_post.append((sid, dayv, sportv, titlev, contentv, createdv, menuid, row_idx))

    if not to_post:
        await update.message.reply_text("업로드할 새 글이 없어(이미 올린 글은 제외됨).")
        return

    delay_sec = float(os.getenv("CAFE_POST_DELAY_SEC", "7"))
    backoff_sec = float(os.getenv("CAFE_RATE_LIMIT_BACKOFF_SEC", "60"))
    retries = int(os.getenv("CAFE_RATE_LIMIT_RETRIES", "1"))

    def _is_rate_limited(err: str) -> bool:
        e = (err or "")
        if "연속" in e and "등록" in e:
            return True
        if "HTTP_403" in e and '"code":"999"' in e:
            return True
        return False

    ok_cnt, fail_cnt = 0, 0
    for (sid, dayv, sportv, titlev, contentv, createdv, menuid, row_idx) in to_post:
        if not menuid:
            fail_cnt += 1
            ws_log.append_row(
                [sid, dayv, sportv, NAVER_CAFE_CLUBID, menuid, "", now_kst().isoformat(), "NO_MENU_ID", titlev, "", ""],
                value_input_option="RAW",
                table_range="A1",
            )
            continue

        content_txt = (contentv or "").strip()
        if not content_txt:
            fail_cnt += 1
            status = "NO_BODY" if mode == "deep" else "NO_SIMPLE"
            ws_log.append_row(
                [sid, dayv, sportv, NAVER_CAFE_CLUBID, menuid, "", now_kst().isoformat(), status, titlev, "", ""],
                value_input_option="RAW",
                table_range="A1",
            )
            continue

        # 줄바꿈/HTML 태그 정규화 (body에 <br> 등이 섞여 있을 수 있음)
        content_norm = str(content_txt)
        content_norm = content_norm.replace("\r\n", "\n").replace("\r", "\n")
        content_norm = re.sub(r"<br\s*/?>", "\n", content_norm, flags=re.I)
        content_norm = re.sub(r"</(p|div|li)>", "\n", content_norm, flags=re.I)
        content_norm = re.sub(r"<[^>]+>", "", content_norm)
        content_norm = content_norm.replace("&nbsp;", " ")
        content_norm = content_norm.strip()

        safe_content = html.escape(content_norm)

        # 네이버 카페 필터링을 피하기 위해 따옴표/스타일 속성 없이 center + <br>만 사용
        _lines = safe_content.split("\n") if safe_content else [""]
        _html_lines = [ln if ln.strip() else "&nbsp;" for ln in _lines]
        content_html = "<center>" + "<br>".join(_html_lines) + "</center>"
        content_plain = content_norm  # fallback

        subject = (titlev or "").strip() or f"{sportv} 분석"
        if len(subject) > 80:
            subject = subject[:80]

        attempt = 0
        tried_plain = False
        while True:
            success, info = _naver_cafe_post(
                subject=subject,
                content=(content_plain if tried_plain else content_html),
                clubid=NAVER_CAFE_CLUBID,
                menuid=menuid,
            )

            if success:
                article_id = str(info).strip()
                posted_at = now_kst().isoformat()
                article_url = ""
                if article_id.isdigit():
                    article_url = f"{NAVER_CAFE_BASE_URL}/{article_id}"
                url = article_url if mode != "deep" else ""
                deep_url = article_url if mode == "deep" else ""

                ok_cnt += 1
                ws_log.append_row(
                    [sid, dayv, sportv, NAVER_CAFE_CLUBID, menuid, article_id, posted_at, "OK", subject, url, deep_url],
                    value_input_option="RAW",
                    table_range="A1",
                )
                # export 시트에도 업로드된 링크를 기록(가능할 때만)
                try:
                    if i_cafe_title != -1:
                        ws.update(range_name=f"{_col_letter(i_cafe_title + 1)}{row_idx}", values=[[subject]])
                    if mode == "deep":
                        if i_cafe_url_deep != -1 and deep_url:
                            ws.update(range_name=f"{_col_letter(i_cafe_url_deep + 1)}{row_idx}", values=[[deep_url]])
                    else:
                        if i_cafe_url != -1 and url:
                            ws.update(range_name=f"{_col_letter(i_cafe_url + 1)}{row_idx}", values=[[url]])
                except Exception as e:
                    print(f"[CAFE][EXPORT_LINK] 업데이트 실패({sid}): {e}")

                break

            # 999 내부 오류가 HTML 파싱/필터링 문제일 수도 있어서 plain 텍스트로 1회 폴백
            if (not tried_plain) and ("HTTP_403" in (info or "")) and ('"code":"999"' in (info or "")):
                tried_plain = True
                continue

            # 연속등록/레이트리밋은 백오프 후 재시도
            if _is_rate_limited(info) and attempt < retries:
                attempt += 1
                await asyncio.sleep(backoff_sec)
                continue

            fail_cnt += 1
            ws_log.append_row(
                [sid, dayv, sportv, NAVER_CAFE_CLUBID, menuid, "", now_kst().isoformat(), info, subject, "", ""],
                value_input_option="RAW",
                table_range="A1",
            )
            break

        await asyncio.sleep(delay_sec)

    await update.message.reply_text(f"카페 업로드 완료({mode}): 성공 {ok_cnt} / 실패 {fail_cnt} (중복은 제외됨)")


def _log_httpx_exception(prefix: str, e: Exception) -> None:
    """Render 로그에 httpx 예외(특히 403/URL)를 확실히 남긴다."""
    try:
        req = getattr(e, "request", None)
        resp = getattr(e, "response", None)
        if req is not None:
            print(f"{prefix} request_url={getattr(req, 'url', '')}")
        if resp is not None:
            try:
                print(f"{prefix} status_code={resp.status_code} url={resp.url}")
                txt = (resp.text or "")
                if txt:
                    print(f"{prefix} resp_text_head={txt[:300]}")
            except Exception:
                pass
    except Exception:
        pass

    print(f"{prefix} exc={repr(e)}")
    traceback.print_exc()


# ----------------------------
# HTTP helpers (Mazgtv anti-bot 대응: 브라우저 헤더 + 쿠키 워밍업)
# ----------------------------
MAZ_BASE_URL = os.getenv("MAZ_BASE_URL", "https://mazgtv3.com").rstrip("/")
MAZ_LIST_API = os.getenv("MAZ_LIST_API", f"{MAZ_BASE_URL}/api/board/list")


def build_maz_list_params(*, page: int = 1, perpage: int = 15, type_: str = "event",
                          boardType: int = 4, category: int = 0,
                          secretFlag: int = 0, fixFlag: bool = True) -> dict:
    """mazgtv2 list API 파라미터를 표준화한다."""
    return {
        "page": page,
        "perpage": perpage,
        "type": type_,
        "boardType": boardType,
        "category": category,
        "secretFlag": secretFlag,
        "fixFlag": str(fixFlag).lower(),  # maz는 true/false 문자열을 쓰는 경우가 있음
    }


BROWSER_HEADERS = {
    "User-Agent": os.getenv(
        "MAZ_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": f"{MAZ_BASE_URL}/",
    "Origin": MAZ_BASE_URL,
    "Connection": "keep-alive",
}

async def _maz_warmup(client: httpx.AsyncClient) -> None:
    """API 호출 전 1회 워밍업으로 쿠키/세션 세팅을 유도한다.
    403이 계속이면 사이트 측(WAF/차단)에서 서버 IP를 막았을 가능성이 큼.
    """
    try:
        await client.get(f"{MAZ_BASE_URL}/", headers=BROWSER_HEADERS, timeout=15.0)
    except Exception:
        return

import math
import io
import zipfile
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote_plus, unquote_plus
from openai import OpenAI

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)

from datetime import datetime, timedelta, date

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    TypeHandler,
    ApplicationHandlerStop,
    ContextTypes,
    filters,
)

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ───────────────── 기본 설정 ─────────────────
TOKEN = os.getenv("BOT_TOKEN")
APP_URL = (os.getenv("APP_URL") or "").strip().rstrip("/")
WEBHOOK_PATH = (os.getenv("TELEGRAM_WEBHOOK_PATH") or "telegram").strip().lstrip("/")
CHANNEL_ID = (os.getenv("CHANNEL_ID") or "").strip()  # 예: @채널아이디 또는 -100xxxxxxxxxxxx

# 🔴 여기만 네 봇 유저네임으로 수정하면 됨 (@ 빼고)
BOT_USERNAME = "castlive_bot"  # 예: @castlive_bot 이라면 "castlive_bot"

# ───────────────── Telegram 업데이트 중복 방지 ─────────────────
# Render 슬립/재기동 직후 텔레그램이 동일 업데이트를 재전송하는 경우,
# 같은 명령이 2번 실행/응답되는 현상을 방지한다.
TG_DEDUP_ENABLED = (os.getenv("TG_DEDUP_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off"))
TG_DEDUP_TTL_SEC = int(os.getenv("TG_DEDUP_TTL_SEC", "900"))  # 기본 15분
TG_DEDUP_MAX = int(os.getenv("TG_DEDUP_MAX", "5000"))  # 메모리 보호용 상한

_RECENT_UPDATE_IDS: dict[int, float] = {}
_RECENT_UPDATE_LOCK = asyncio.Lock()


async def _dedup_update_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """중복 update_id 처리 방지. 중복이면 해당 업데이트의 나머지 핸들러 실행을 중단한다."""
    if not TG_DEDUP_ENABLED:
        return

    uid = getattr(update, "update_id", None)
    if uid is None:
        return

    now_ts = time.time()

    async with _RECENT_UPDATE_LOCK:
        # 1) TTL 만료된 항목 제거 (삽입 순서 보장: dict는 py3.7+에서 insertion ordered)
        while _RECENT_UPDATE_IDS:
            oldest_uid, oldest_ts = next(iter(_RECENT_UPDATE_IDS.items()))
            if (now_ts - oldest_ts) > TG_DEDUP_TTL_SEC:
                _RECENT_UPDATE_IDS.pop(oldest_uid, None)
            else:
                break

        # 2) 크기 상한 유지
        while len(_RECENT_UPDATE_IDS) >= TG_DEDUP_MAX:
            oldest_uid = next(iter(_RECENT_UPDATE_IDS.keys()))
            _RECENT_UPDATE_IDS.pop(oldest_uid, None)

        # 3) 중복 체크
        if uid in _RECENT_UPDATE_IDS:
            raise ApplicationHandlerStop

        _RECENT_UPDATE_IDS[uid] = now_ts


# 🔹 Gemini API 키 (환경변수에 설정)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# 🔹 관리자 ID 목록 (쉼표로 여러 명 가능) 예: "123456789,987654321"
_admin_ids_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [
    int(x.strip())
    for x in _admin_ids_raw.split(",")
    if x.strip().isdigit()
]


def is_admin(update: Update) -> bool:
    """이 명령어를 누가 호출했는지 확인해서, 관리자면 True 리턴"""
    if not ADMIN_IDS:
        # ADMIN_IDS를 안 넣었으면 그냥 모두 허용 (테스트용)
        return True
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS)


# ───────────────── 날짜 헬퍼 ─────────────────

def get_kst_now() -> datetime:
    """한국 시간 기준 현재 시각 (UTC+9)"""
    return now_kst()


def get_date_labels():
    """
    오늘 / 내일 날짜를 'M.DD' 형식으로 돌려줌
    예: ( '11.14', '11.15' )
    """
    now_kst = get_kst_now().date()
    today = now_kst
    tomorrow = now_kst + timedelta(days=1)

    today_str = f"{today.month}.{today.day:02d}"
    tomorrow_str = f"{tomorrow.month}.{tomorrow.day:02d}"
    return today_str, tomorrow_str

def get_tomorrow_mmdd_str() -> str:
    """
    mazgtv 테이블의 '11-28 (금) 02:45' 같은 날짜에서
    앞부분 'MM-DD' 와 비교하기 위한 내일 날짜 문자열 생성 (예: '11-28')
    """
    tomorrow = get_kst_now().date() + timedelta(days=1)
    return f"{tomorrow.month:02d}-{tomorrow.day:02d}"

def get_tomorrow_keywords():
    """
    해외분석 리스트에서 '내일 경기'만 필터링하기 위한 키워드 세트 생성.
    - '내일'
    - 11.28 / 11-28 / 11/28 같은 여러 날짜 포맷
    """
    tomorrow = get_kst_now().date() + timedelta(days=1)
    m = tomorrow.month
    d = tomorrow.day

    md_dot_1 = f"{m}.{d}"
    md_dot_2 = f"{m}.{d:02d}"
    md_dash_1 = f"{m}-{d}"
    md_dash_2 = f"{m}-{d:02d}"
    md_slash_1 = f"{m}/{d}"
    md_slash_2 = f"{m}/{d:02d}"

    return {
        "내일",
        md_dot_1, md_dot_2,
        md_dash_1, md_dash_2,
        md_slash_1, md_slash_2,
    }


def get_menu_caption() -> str:
    """메인 메뉴 설명 텍스트 (오늘/내일 날짜 자동 반영)"""
    today_str, tomorrow_str = get_date_labels()
    return (
        "📌 스포츠 정보&분석 공유방 메뉴 안내\n\n"
        "1️⃣ 실시간 무료 중계 - 라이브 중계 바로가기\n"
        f"2️⃣ {today_str} 경기 분석픽 - 종목별로 {today_str} 경기 분석을 확인하세요\n"
        f"3️⃣ {tomorrow_str} 경기 분석픽 - 종목별로 {tomorrow_str} 경기 분석을 확인하세요\n"
        "4️⃣ 스포츠 뉴스 요약 - 주요 이슈 & 뉴스 요약 정리\n\n"
        "아래 버튼을 눌러 원하는 메뉴를 선택하세요 👇"
    )


# ───────────────── 분석/뉴스 데이터 (예시) ─────────────────

ANALYSIS_TODAY = {
    "축구": [],
    "농구": [],
    "야구": [],
    "배구": [],
}
ANALYSIS_TOMORROW = {
    "축구": [],
    "농구": [],
    "야구": [],
    "배구": [],
}

ANALYSIS_DATA_MAP = {
    "today": ANALYSIS_TODAY,
    "tomorrow": ANALYSIS_TOMORROW,
}



# ───────────────── 뉴스 데이터 ─────────────────
# 'news' 시트에서 로딩되며, 인라인 메뉴(뉴스 요약)에 사용됩니다.
NEWS_DATA = {
    "축구": [],
    "농구": [],
    "야구": [],
    "배구": [],
}

# ───────────────── 다음 스포츠 카테고리 ID 설정 ─────────────────
# DevTools > Network 에서 harmony contents.json 요청 확인 후
# defaultCategoryId3 의 value 를 환경변수에 세팅.
DAUM_CATEGORY_IDS = {
    # 해외축구
    "world_soccer": os.getenv("DAUM_CAT_WORLD_SOCCER", "100032"),

    # 국내축구 (K리그)
    "soccer_kleague": os.getenv("DAUM_CAT_SOCCER_KLEAGUE", "1027"),

    # 국내야구 (KBO)
    "baseball_kbo": os.getenv("DAUM_CAT_BASEBALL_KBO", "1028"),

    # 해외야구 (MLB)
    "baseball_world": os.getenv("DAUM_CAT_BASEBALL_WORLD", "1015"),

    # 농구
    "basketball": os.getenv("DAUM_CAT_BASKETBALL", "1029"),

    # 배구
    "volleyball": os.getenv("DAUM_CAT_VOLLEYBALL", "100033"),
}


# ───────────────── 구글 시트 연동 설정 ─────────────────

_gs_client = None  # gspread 클라이언트 캐시용


def get_gs_client():
    """환경변수에서 서비스계정 JSON 읽어서 gspread 클라이언트 생성"""
    global _gs_client
    if _gs_client is not None:
        return _gs_client

    key_raw = os.getenv("GOOGLE_SERVICE_KEY")
    if not key_raw:
        print("[GSHEET] GOOGLE_SERVICE_KEY 환경변수가 없습니다. 시트 연동 건너뜀.")
        return None

    try:
        key_data = json.loads(key_raw)
    except Exception as e:
        print(f"[GSHEET] GOOGLE_SERVICE_KEY JSON 파싱 오류: {e}")
        return None

    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = ServiceAccountCredentials.from_json_keyfile_dict(key_data, scope)
    _gs_client = gspread.authorize(creds)
    print("[GSHEET] gspread 인증 완료")
    return _gs_client


def summarize_text(text: str, max_len: int = 400) -> str:
    """
    (예전용) 아주 단순한 요약: 문장을 잘라서 앞에서부터 max_len까지 자르는 방식.
    """
    text = text.replace("\n", " ").strip()
    sentences = re.split(r'(?<=[\.!?다요])\s+', text)
    result = ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if not result:
            candidate = s
        else:
            candidate = result + " " + s
        if len(candidate) > max_len:
            break
        result = candidate
    if not result:
        result = text[:max_len]
    return result


def clean_daum_body_text(text: str) -> str:
    """
    다음 뉴스 본문에서 번역/요약 UI, 언어 목록, 기자 크레딧/사진 설명 등
    불필요한 문장을 최대한 제거하고 기사 본문만 남긴다.
    """
    if not text:
        return ""

    # 1단계: 줄 단위로 나누고, 빈 줄 제거
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    blacklist = [
        "음성으로 듣기",
        "음성 재생",
        "음성재생 설정",
        "번역 설정",
        "번역 beta",
        "Translated by",
        "전체 맥락을 이해하기 위해서는 본문 보기를 권장합니다.",
        "요약문이므로 일부 내용이 생략될 수 있습니다.",
        "요약본이 자동요약 기사 제목과 주요 문장을 기반으로 자동요약한 결과입니다",
        "기사 제목과 주요 문장을 기반으로 자동요약한 결과입니다",
        # 언어 목록 키워드
        "한국어 - English",
        "한국어 - 영어",
        "English",
        "日本語",
        "简体中文",
        "Deutsch",
        "Русский",
        "Español",
        "العربية",
        "bahasa Indonesia",
        "ภาษาไทย",
        "Türkçe",
    ]

    clean_lines = []
    for l in lines:
        # 1) 공통 블랙리스트
        if any(b in l for b in blacklist):
            continue

        # 2) 사진/기사 크레딧 한 줄 통째로 날리기
        if re.match(r"^\[[^]]{2,60}\]\s*[^ ]{1,20}\s*(기자|통신원|특파원)?\s*$", l):
            continue

        clean_lines.append(l)

    text = " ".join(clean_lines)

    # 3단계: 본문 안에 끼어 있는 크레딧 패턴 제거
    text = re.sub(
        r"\[[^]]{2,60}(일보|뉴스|코리아|KOREA|포포투|베스트 일레븐)[^]]*?\]\s*[^ ]{1,20}\s*(기자|통신원|특파원)?",
        "",
        text,
    )
    text = re.sub(
        r"\[[^]]{2,60}\]\s*[^ ]{1,20}\s*(기자|통신원|특파원)",
        "",
        text,
    )

    # 4단계: "요약보기 자동요약" 꼬리 제거
    text = re.sub(r"요약보기\s*자동요약.*$", "", text)

    # 5단계: 공백 정리
    text = re.sub(r"\s{2,}", " ", text).strip()

    return text


def remove_title_prefix(title: str, body: str) -> str:
    """
    본문이 제목으로 시작하면 그 부분을 잘라낸다.
    (제목이 그대로 summary 에 반복되는 현상 완화용)
    """
    if not title or not body:
        return body

    t = title.strip().strip('"“”')
    b = body.strip()

    candidates = [
        t,
        f'"{t}"',
        f"“{t}”",
    ]

    for cand in candidates:
        if b.startswith(cand):
            return b[len(cand):].lstrip(" -–:·, \"\'")

    return b

def parse_maz_overseas_row(tr) -> dict | None:
    """
    mazgtv 해외분석 테이블의 <tr> 하나에서
    리그명 / 홈팀 / 원정팀 / 킥오프 시간 / 상세 링크를 추출한다.
    """
    tds = tr.find_all("td")
    if len(tds) < 3:
        return None

    # 홈팀
    home_parts = list(tds[0].stripped_strings)
    home_team = home_parts[0] if home_parts else ""

    # 가운데: [리그, VS, 날짜/시간] 구조라고 가정
    center_parts = list(tds[1].stripped_strings)
    league = center_parts[0] if center_parts else ""
    kickoff = center_parts[-1] if center_parts else ""

    # 원정팀
    away_parts = list(tds[2].stripped_strings)
    away_team = away_parts[0] if away_parts else ""

    # 상세 링크 (tr 안에 있는 첫 번째 <a href>)
    a = tr.select_one("a[href]") or tr.find("a", href=True)
    url = a["href"].strip() if a and a.get("href") else ""

    return {
        "league": league.strip(),
        "home": home_team.strip(),
        "away": away_team.strip(),
        "kickoff": kickoff.strip(),
        "url": url,
    }

def _load_analysis_sheet(sh, sheet_name: str) -> dict:
    """
    구글시트에서 한 탭(today / tomorrow)을 읽어서
    { sport: [ {id,title,summary}, ... ] } 구조로 변환
    """
    try:
        ws = sh.worksheet(sheet_name)
    except Exception as e:
        print(f"[GSHEET] 시트 '{sheet_name}' 열기 실패: {e}")
        return {}

    rows = ws.get_all_values()
    if not rows:
        return {}

    header = rows[0]
    idx_sport = 0
    idx_id = 1
    idx_title = 2
    idx_summary = 3

    def safe_index(name, default):
        try:
            return header.index(name)
        except ValueError:
            return default

    idx_sport = safe_index("sport", idx_sport)
    idx_id = safe_index("id", idx_id)
    idx_title = safe_index("title", idx_title)
    idx_summary = safe_index("summary", idx_summary)

    data: dict[str, list[dict]] = {}

    for row in rows[1:]:
        if len(row) <= idx_title:
            continue

        sport = (row[idx_sport] if len(row) > idx_sport else "").strip()
        if not sport:
            continue

        item_id = (row[idx_id] if len(row) > idx_id else "").strip()
        title = (row[idx_title] if len(row) > idx_title else "").strip()
        summary = (row[idx_summary] if len(row) > idx_summary else "").strip()

        if not title:
            continue

        if not item_id:
            cur_len = len(data.get(sport, []))
            item_id = f"{sport}_{cur_len + 1}"

        entry = {
            "id": item_id,
            "title": title,
            "summary": summary,
        }
        data.setdefault(sport, []).append(entry)

    return data


def reload_analysis_from_sheet():
    """
    구글시트에서 today / tomorrow 탭을 읽어서
    ANALYSIS_TODAY / ANALYSIS_TOMORROW / ANALYSIS_DATA_MAP 갱신
    """
    global ANALYSIS_TODAY, ANALYSIS_TOMORROW, ANALYSIS_DATA_MAP

    client = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")

    if not client or not spreadsheet_id:
        print("[GSHEET] 시트 클라이언트 또는 SPREADSHEET_ID 없음 → 기존 하드코딩 데이터 사용")
        return

    try:
        sh = client.open_by_key(spreadsheet_id)
    except Exception as e:
        print(f"[GSHEET] 스프레드시트 열기 실패: {e}")
        return

    sheet_today_name = os.getenv("SHEET_TODAY_NAME", "today")
    sheet_tomorrow_name = os.getenv("SHEET_TOMORROW_NAME", "tomorrow")

    print(f"[GSHEET] '{sheet_today_name}' / '{sheet_tomorrow_name}' 탭에서 분석 데이터 로딩 시도")

    try:
        today_data = _load_analysis_sheet(sh, sheet_today_name)
        tomorrow_data = _load_analysis_sheet(sh, sheet_tomorrow_name)
    except Exception as e:
        print(f"[GSHEET] 시트 데이터 로딩 중 오류: {e}")
        return

    ANALYSIS_TODAY = today_data
    ANALYSIS_TOMORROW = tomorrow_data

    ANALYSIS_DATA_MAP = {
        "today": ANALYSIS_TODAY,
        "tomorrow": ANALYSIS_TOMORROW,
    }

    print("[GSHEET] ANALYSIS_TODAY / ANALYSIS_TOMORROW 갱신 완료")



def reload_news_from_sheet():
    """구글시트 'news' 탭을 읽어 NEWS_DATA 전역을 갱신한다.

    - /syncsheet, /reloadsheet, main() 시작 시 호출됨
    - news 시트 포맷: [sport, id, title, summary]
    """
    global NEWS_DATA

    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")

    if not (client_gs and spreadsheet_id):
        print("[GSHEET] 구글시트 설정(GOOGLE_SERVICE_KEY 또는 SPREADSHEET_ID)이 없어 NEWS_DATA 로딩 생략")
        return

    sheet_news_name = os.getenv("SHEET_NEWS_NAME", "news")

    try:
        sh = client_gs.open_by_key(spreadsheet_id)
    except Exception as e:
        print(f"[GSHEET] 스프레드시트를 열지 못했습니다 (NEWS): {e}")
        return

    try:
        print(f"[GSHEET] '{sheet_news_name}' 탭에서 뉴스 데이터 로딩 시도")
        news_data = _load_analysis_sheet(sh, sheet_news_name)

        if isinstance(news_data, dict):
            NEWS_DATA = news_data
        else:
            NEWS_DATA = {
                "축구": [],
                "농구": [],
                "야구": [],
                "배구": [],
            }

        print("[GSHEET] NEWS_DATA 갱신 완료")
    except Exception as e:
        print(f"[GSHEET] NEWS_DATA 로딩 실패: {e}")
        # 실패 시에도 기존 값 유지

def append_analysis_rows(day_key: str, rows: list[list[str]]) -> bool:
    """
    분석 데이터를 today / tomorrow 탭에 추가하는 공용 함수.
    rows: [ [sport, "", title, summary], ... ]
    """
    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")

    if not (client_gs and spreadsheet_id):
        print("[GSHEET][ANALYSIS] 설정 없음 → 저장 불가")
        return False

    sheet_today_name = os.getenv("SHEET_TODAY_NAME", "today")
    sheet_tomorrow_name = os.getenv("SHEET_TOMORROW_NAME", "tomorrow")
    sheet_name = sheet_today_name if day_key == "today" else sheet_tomorrow_name

    try:
        sh = client_gs.open_by_key(spreadsheet_id)
        ws = sh.worksheet(sheet_name)
    except Exception as e:
        print(f"[GSHEET][ANALYSIS] 시트 '{sheet_name}' 열기 실패: {e}")
        return False

    try:
        ws.append_rows(rows, value_input_option="RAW", table_range="A1")
        print(f"[GSHEET][ANALYSIS] {sheet_name} 에 {len(rows)}건 추가")
        return True
    except Exception as e:
        print(f"[GSHEET][ANALYSIS] append_rows 오류: {e}")
        return False

def _get_ws_by_name(sh, name: str):
    try:
        return sh.worksheet(name)
    except Exception:
        return None

def get_site_export_ws():
    """
    site_export 탭 워크시트 반환.
    없으면 생성 시도(권한/환경에 따라 실패 가능).
    """
    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not (client_gs and spreadsheet_id):
        return None

    sheet_name = os.getenv("SHEET_SITE_EXPORT_NAME", "site_export")

    try:
        sh = client_gs.open_by_key(spreadsheet_id)
        ws = _get_ws_by_name(sh, sheet_name)
        if ws:
            ensure_export_header(ws)
            return ws

        # 없으면 생성 시도
        ws = sh.add_worksheet(title=sheet_name, rows=2000, cols=max(10, len(EXPORT_HEADER)))
        # 헤더 세팅
        ws.update(range_name="A1", values=[SITE_EXPORT_HEADER])
        return ws

    except Exception as e:
        print(f"[GSHEET][SITE_EXPORT] 워크시트 준비 실패: {e}")
        return None


# ───────────────── site_export 저장 ─────────────────

SITE_EXPORT_SHEET_NAME = os.getenv("SHEET_SITE_EXPORT_NAME", "site_export")
SITE_EXPORT_HEADER = ["day", "sport", "src_id", "title", "body", "createdAt", "simple", "cafe_title", "cafe_url", "comments", "cafe_url_deep", "deep_comments"]
# export 탭 분리: export_today / export_tomorrow
EXPORT_TODAY_SHEET_NAME = os.getenv("SHEET_EXPORT_TODAY_NAME", "export_today")
EXPORT_TOMORROW_SHEET_NAME = os.getenv("SHEET_EXPORT_TOMORROW_NAME", "export_tomorrow")

EXPORT_HEADER = SITE_EXPORT_HEADER  # 동일 헤더 사용



# ───────────────── Naver Cafe → Google Sheet (youtoo 탭) ─────────────────
# youtoo 탭: 카페 게시글 백업/수집용

YOUTOO_SHEET_NAME = (os.getenv("YOUTOO_SHEET_NAME") or "youtoo").strip()

# ✅ 봇이 자동으로 수집/갱신하는 컬럼(A~K)
# (youtoo()에서 생성하는 new_rows 순서와 반드시 동일해야 함)
YOUTOO_AUTO_HEADER = [
    "src_id",
    "경기",
    "댓글수",
    "조회수",
    "좋아요",
    "본문링크",
    "첫댓글내용",
    "첫댓글작성자",
    "첫댓글시간",
    "게시시간(날짜)",
    "별명",
]

# ✅ 사람이 수기로 입력하는 컬럼(L~M) - 봇이 절대 덮어쓰지 않음
YOUTOO_MANUAL_HEADER = [
    "적중건제출여부",
    "지급여부",
]

# 전체 헤더(A~M)
YOUTOO_HEADER = YOUTOO_AUTO_HEADER + YOUTOO_MANUAL_HEADER

def _col_letter(n: int) -> str:
    """1-indexed column number -> A1 column letter."""
    s = ""
    x = int(n)
    while x > 0:
        x, r = divmod(x - 1, 26)
        s = chr(65 + r) + s
    return s

# 자동 갱신 범위(A~K)의 끝 컬럼(기본: K)
YOUTOO_AUTO_END_COL = _col_letter(len(YOUTOO_AUTO_HEADER))

# 과거 버전/표기 차이 호환(자동 마이그레이션용)
_YOUTOO_COL_ALIASES: dict[str, list[str]] = {
    "첫댓글내용": ["첫댓글"],
    "첫댓글작성자": ["첫댓글별명", "첫댓글닉네임"],
    "적중건제출여부": ["적중 제출 여부", "적중제출여부", "적중여부"],
    "지급여부": ["지급 여부"],
}

def _youtoo_find_header_index(old_header: list[str], col: str) -> int | None:
    """old_header에서 col(또는 별칭)의 위치를 찾는다."""
    try:
        return old_header.index(col)
    except ValueError:
        pass
    for alt in _YOUTOO_COL_ALIASES.get(col, []):
        try:
            return old_header.index(alt)
        except ValueError:
            continue
    return None


def ensure_youtoo_header(ws) -> None:
    """youtoo 시트 헤더를 최신 스펙으로 맞춘다.

    - A~K: 봇이 자동 수집/갱신하는 컬럼
    - L~M: 사람이 수기로 입력하는 컬럼(적중건제출여부/지급여부) → ✅ 봇이 절대 덮어쓰지 않음

    헤더가 어긋난 과거 버전(구/신 헤더 혼재)도 가능한 범위 내에서 자동 마이그레이션한다.
    """
    try:
        values = ws.get_all_values()
    except Exception:
        values = []

    # 시트가 비어있으면 헤더부터 세팅
    if not values:
        try:
            if getattr(ws, "col_count", 0) < len(YOUTOO_HEADER):
                ws.resize(cols=len(YOUTOO_HEADER))
        except Exception:
            pass
        ws.update(range_name="A1", values=[YOUTOO_HEADER])
        return

    # get_all_values()는 "헤더 행의 빈 셀"을 끝까지 반환하지 않을 수 있으므로,
    # 전체 데이터에서 가장 긴 열 길이를 기준으로 헤더를 패딩한다.
    max_cols = max([len(r) for r in values] + [len(YOUTOO_HEADER)])
    raw_header = list(values[0] or [])
    header = [str(c).strip() for c in raw_header] + [""] * (max_cols - len(raw_header))

    # 필요한 경우에만 cols 확장(절대 축소 금지)
    try:
        desired_cols = max(len(YOUTOO_HEADER), max_cols)
        if getattr(ws, "col_count", 0) < desired_cols:
            ws.resize(cols=desired_cols)
    except Exception:
        pass

    auto_len = len(YOUTOO_AUTO_HEADER)
    auto_match = [str(c).strip() for c in header[:auto_len]] == YOUTOO_AUTO_HEADER

    # ✅ A~K 헤더가 이미 맞으면: L/M 헤더만 보정하고 끝낸다. (수기 데이터 보호)
    if auto_match:
        if len(header) < len(YOUTOO_HEADER):
            header += [""] * (len(YOUTOO_HEADER) - len(header))

        for i, name in enumerate(YOUTOO_MANUAL_HEADER):
            col_idx_1based = auto_len + 1 + i  # L=12, M=13
            cur = str(header[auto_len + i] or "").strip()
            if cur == name:
                continue
            try:
                ws.update_cell(1, col_idx_1based, name)
            except Exception:
                try:
                    ws.update(range_name=f"{_col_letter(col_idx_1based)}1", values=[[name]])
                except Exception:
                    pass
        return

    # ✅ 헤더가 과거 버전이면: 가능한 범위에서 전체 마이그레이션(수기 L/M도 보존)
    old_header = header  # 패딩된 헤더

    new_rows: list[list[str]] = []
    for row in values[1:]:
        rp = list(row) + [""] * (max_cols - len(row))
        nr: list[str] = []
        for col_name in YOUTOO_HEADER:
            idx = _youtoo_find_header_index(old_header, col_name)
            if idx is None:
                # 헤더명이 비어있던 경우를 대비해, L/M은 "위치 기반"으로도 복원 시도
                if col_name in YOUTOO_MANUAL_HEADER:
                    pos = YOUTOO_HEADER.index(col_name)
                    nr.append(rp[pos] if pos < len(rp) else "")
                else:
                    nr.append("")
            else:
                nr.append(rp[idx] if idx < len(rp) else "")

        # 완전 빈 행은 건너뛰기(공백 공간 누적 방지)
        if any((x or "").strip() for x in nr):
            new_rows.append(nr)

    try:
        ws.clear()
    except Exception:
        pass

    try:
        if getattr(ws, "col_count", 0) < len(YOUTOO_HEADER):
            ws.resize(cols=len(YOUTOO_HEADER))
    except Exception:
        pass

    ws.update(range_name="A1", values=[YOUTOO_HEADER] + new_rows, value_input_option="RAW")


def get_youtoo_ws():
    """youtoo 탭 워크시트 반환(없으면 생성 + 헤더 세팅)."""
    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not (client_gs and spreadsheet_id):
        return None

    try:
        sh = client_gs.open_by_key(spreadsheet_id)
        ws = _get_ws_by_name(sh, YOUTOO_SHEET_NAME)
        if not ws:
            ws = sh.add_worksheet(title=YOUTOO_SHEET_NAME, rows=2000, cols=max(10, len(YOUTOO_HEADER)))

        # ✅ cols는 "필요할 때만 확장" (절대 축소 금지: 수기 L/M 보호)
        try:
            if getattr(ws, "col_count", 0) < len(YOUTOO_HEADER):
                ws.resize(cols=len(YOUTOO_HEADER))
        except Exception:
            pass

        ensure_youtoo_header(ws)
        return ws
    except Exception as e:
        print(f"[GSHEET][YOUTOO] 워크시트 준비 실패({YOUTOO_SHEET_NAME}): {e}")
        return None


def upsert_youtoo_rows_top(rows: list[list[str]]) -> tuple[bool, int, int]:
    """youtoo 시트에 rows를 upsert 하되, ✅ 신규는 2행(헤더 아래)에 삽입해서 '위로 업데이트'되게 만든다.

    반환: (ok, inserted_count, updated_count)

    - src_id 기준으로 중복을 판단한다.
    - 이미 존재하면 해당 행을 덮어쓴다(댓글수/조회수/좋아요 등이 갱신될 수 있으므로).
    - 신규는 insert_rows(row=2)로 상단에 붙인다.
    - ✅ 수기 입력 컬럼(L/M)은 절대 덮어쓰지 않도록 A~K만 업데이트한다.
    """
    if not rows:
        return True, 0, 0

    ws = get_youtoo_ws()
    if not ws:
        return False, 0, 0

    # 헤더 보정/마이그레이션
    ensure_youtoo_header(ws)

    try:
        values = ws.get_all_values()
    except Exception:
        values = []

    if not values:
        ws.update(range_name="A1", values=[YOUTOO_HEADER])
        values = [YOUTOO_HEADER]

    header = [c.strip() for c in values[0]]
    try:
        idx_src = header.index("src_id")
    except ValueError:
        idx_src = 0

    # src_id -> row_number(1-indexed)
    existing_map: dict[str, int] = {}
    for i, row in enumerate(values[1:], start=2):
        if len(row) > idx_src:
            sid = (row[idx_src] or "").strip()
            if sid and sid not in existing_map:
                existing_map[sid] = i

    updated = 0
    to_insert: list[list[str]] = []
    seen: set[str] = set()

    # 1) 기존 행 업데이트(삽입 전에 수행해야 row index가 흔들리지 않음)
    for r in rows:
        if not r:
            continue
        rr = list(r)
        if len(rr) < len(YOUTOO_HEADER):
            rr += [""] * (len(YOUTOO_HEADER) - len(rr))
        elif len(rr) > len(YOUTOO_HEADER):
            rr = rr[: len(YOUTOO_HEADER)]

        sid = (rr[idx_src] or "").strip()
        if (not sid) or (sid in seen):
            continue
        seen.add(sid)

        if sid in existing_map:
            row_num = existing_map[sid]
            try:
                rr_auto = rr[: len(YOUTOO_AUTO_HEADER)]
                ws.update(
                    range_name=f"A{row_num}:{YOUTOO_AUTO_END_COL}{row_num}",
                    values=[rr_auto],
                    value_input_option="RAW",
                )
                updated += 1
            except Exception as e:
                print(f"[GSHEET][YOUTOO] update 실패(src_id={sid}): {e}")
        else:
            to_insert.append(rr)

    inserted = 0
    if to_insert:
        try:
            # ✅ 신규는 맨 위(2행)에 넣어서 최신이 위로 오게 한다.
            ws.insert_rows(to_insert, row=2, value_input_option="RAW")
            inserted = len(to_insert)
        except Exception as e:
            print(f"[GSHEET][YOUTOO] insert_rows 오류: {e}")
            return False, inserted, updated

    print(f"[GSHEET][YOUTOO] {YOUTOO_SHEET_NAME}: inserted={inserted}, updated={updated}")
    return True, inserted, updated

  # createdAt(과거 creatadAt 오타 시트도 호환)

def _ensure_header(ws, header: list[str]) -> None:
    """시트 헤더를 안전하게 보정한다.

    - 시트가 비어있으면 header를 1행에 세팅
    - 기존 헤더가 `header`의 prefix(구버전)라면, **데이터는 건드리지 않고** 1행 헤더만 최신으로 확장
    - 그 외(사용자가 임의로 바꾼 헤더 등)는 강제 변경하지 않는다.
    """
    try:
        first = ws.row_values(1)  # 1행만 읽어 quota 부담 최소화
    except Exception:
        first = []

    try:
        first_norm = [c.strip() for c in (first or []) if str(c).strip() != ""]
        if not first_norm:
            ws.update(range_name="A1", values=[header])
            return

        # 이미 최신 헤더(또는 그 이상)면 OK
        if first_norm[: len(header)] == header:
            return

        # 구버전(예: 7컬럼) → 최신(예: 8컬럼) 확장: 기존 헤더가 최신 헤더의 prefix면 헤더만 업데이트
        if header[: len(first_norm)] == first_norm:
            ws.update(range_name="A1", values=[header])
            return
    except Exception as e:
        print(f"[GSHEET][EXPORT] 헤더 확인/보정 실패: {e}")
        return


def ensure_export_schema(ws, header: list[str]) -> None:
    """export 계열 시트의 헤더/컬럼 순서를 보정한다.

    - 헤더가 비어있으면 최신 header로 세팅
    - 헤더에 필요한 컬럼들이 존재하지만 순서가 다르면: **데이터를 컬럼명 기준으로 재배치**하여 header 순서로 맞춤
    - 그 외(전혀 다른 시트 등)는 헤더만 최신으로 갱신
    """
    try:
        first = ws.row_values(1)
    except Exception:
        first = []

    # 1) 비어있으면 헤더만 세팅
    first_norm = [str(c).strip() for c in (first or []) if str(c).strip() != ""]
    if not first_norm:
        try:
            ws.update(range_name="A1", values=[header])
        except Exception:
            ws.update("A1", [header])
        return

    # 2) 이미 최신이면 OK
    if first_norm[: len(header)] == header:
        return

    # 3) 순서가 다르면 "컬럼명 기준"으로 재배치 (안전한 경우만)
    # - src_id가 있어야 export 시트로 간주
    if "src_id" in first_norm:
        try:
            values = ws.get_all_values()
            if not values:
                ws.update(range_name="A1", values=[header])
                return
            old_header = [str(c).strip() for c in (values[0] or [])]
            old_map = {name: idx for idx, name in enumerate(old_header) if name}

            # header에 있는 컬럼 중 old_header에 최소한 절반 이상이 존재하면 "같은 시트"로 보고 재배치
            overlap = sum(1 for k in header if k in old_map)
            if overlap >= max(3, int(len(header) * 0.5)):
                new_values = [header]
                for row in values[1:]:
                    new_row = []
                    for col_name in header:
                        oi = old_map.get(col_name, -1)
                        new_row.append(row[oi] if (oi != -1 and oi < len(row)) else "")
                    new_values.append(new_row)

                end_col = _col_letter(len(header))
                rng = f"A1:{end_col}{len(new_values)}"
                try:
                    ws.update(range_name=rng, values=new_values)
                except Exception:
                    ws.update(rng, new_values)
                return
        except Exception as e:
            print(f"[GSHEET][EXPORT] 헤더/컬럼 재배치 실패: {e}")

    # 4) 마지막 fallback: 헤더만 최신으로 갱신
    try:
        ws.update(range_name="A1", values=[header])
    except Exception:
        ws.update("A1", [header])


def append_site_export_rows(rows: list[list[str]]) -> bool:
    """
    site_export 탭에 rows를 append.
    rows: [ [day, sport, src_id, title, body, createdAt], ... ]
    """
    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not (client_gs and spreadsheet_id):
        print("[GSHEET][SITE_EXPORT] 설정 없음 → 저장 불가")
        return False

    try:
        sh = client_gs.open_by_key(spreadsheet_id)
        ws = sh.worksheet(SITE_EXPORT_SHEET_NAME)
        _ensure_header(ws, SITE_EXPORT_HEADER)
    except Exception as e:
        print(f"[GSHEET][SITE_EXPORT] 시트 '{SITE_EXPORT_SHEET_NAME}' 열기 실패: {e}")
        return False

    try:
        # rows는 6컬럼([day,sport,src_id,title,body,createdAt]) 또는 7컬럼일 수 있음. simple 자동 생성.
        fixed_rows = []
        for r in rows:
            if not r:
                continue
            rr = list(r)
            if len(rr) == 6:
                rr.append(extract_simple_from_body(rr[4] if len(rr) > 4 else ""))
            elif len(rr) >= 7:
                rr = rr[:7]
            else:
                while len(rr) < 6:
                    rr.append("")
                rr.append(extract_simple_from_body(rr[4] if len(rr) > 4 else ""))
            fixed_rows.append(rr)
        rows = fixed_rows
        ws.append_rows(rows, value_input_option="RAW", table_range="A1")
        print(f"[GSHEET][SITE_EXPORT] {SITE_EXPORT_SHEET_NAME} 에 {len(rows)}건 추가")
        return True
    except Exception as e:
        print(f"[GSHEET][SITE_EXPORT] append_rows 오류: {e}")
        return False


def get_export_ws(sheet_name: str):
    """export_today / export_tomorrow 워크시트 반환(없으면 생성 + 헤더 세팅)."""
    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not (client_gs and spreadsheet_id):
        return None

    try:
        sh = client_gs.open_by_key(spreadsheet_id)
        ws = _get_ws_by_name(sh, sheet_name)
        if not ws:
            ws = sh.add_worksheet(title=sheet_name, rows=2000, cols=10)
        ws.resize(cols=max(10, len(EXPORT_HEADER)))
        _ensure_header(ws, EXPORT_HEADER)
        return ws
    except Exception as e:
        print(f"[GSHEET][EXPORT] 워크시트 준비 실패({sheet_name}): {e}")
        return None


def get_existing_export_src_ids(sheet_name: str) -> set[str]:
    """지정 export 시트에서 src_id 목록을 읽어 중복 저장 방지용 set으로 반환."""
    ws = get_export_ws(sheet_name)
    if not ws:
        return set()

    try:
        values = ws.get_all_values()
        if not values:
            return set()
        header = [c.strip() for c in values[0]]
        if "src_id" not in header:
            return set()
        idx = header.index("src_id")
        out = set()
        for row in values[1:]:
            if len(row) > idx:
                v = (row[idx] or "").strip()
                if v:
                    out.add(v)
        return out
    except Exception as e:
        print(f"[GSHEET][EXPORT] 기존 src_id 로딩 실패({sheet_name}): {e}")
        return set()


def _parse_export_title_parts(title: str) -> dict:
    """export 제목(또는 simple 첫줄)에서 날짜/리그/팀 구간을 가볍게 추출한다.
    - 포맷이 다양하므로 '보조정보'로만 사용하고, 최종 생성은 원문 title을 우선한다.
    """
    t = (title or "").strip()
    out = {"raw": t, "date": "", "league": "", "teams": ""}
    if not t:
        return out
    m = re.match(r"^\s*(\d{1,2}\s*월\s*\d{1,2}\s*일)", t)
    if m:
        out["date"] = m.group(1).replace(" ", "")
    m2 = re.search(r"\[(.*?)\]", t)
    if m2:
        out["league"] = (m2.group(1) or "").strip()
        tail = t.split("]", 1)[1].strip() if "]" in t else ""
    else:
        tail = t
    # 흔한 접미 제거
    tail2 = re.sub(r"(스포츠\s*분석|경기\s*분석|분석\s*글).*?$", "", tail).strip()
    out["teams"] = tail2
    return out



# ───────────────── Export H열(OpenAI 댓글) 생성 ─────────────────

EXPORT_COMMENT_LAST_ERROR = ""


def _set_export_comment_last_error(msg: str) -> None:
    global EXPORT_COMMENT_LAST_ERROR
    EXPORT_COMMENT_LAST_ERROR = (msg or "").strip()[:600]


def _extract_text_from_responses_obj(resp) -> str:
    """OpenAI Responses API 응답에서 텍스트를 최대한 안전하게 추출."""
    if resp is None:
        return ""
    # 1) SDK가 제공하는 output_text
    t = getattr(resp, "output_text", None)
    if isinstance(t, str) and t.strip():
        return t.strip()

    # 2) output 구조를 직접 훑기
    out = getattr(resp, "output", None)
    if isinstance(out, list):
        parts = []
        for item in out:
            content = getattr(item, "content", None)
            if not isinstance(content, list):
                continue
            for c in content:
                # common: {type:"output_text", text:"..."}
                ctype = getattr(c, "type", None)
                ctext = getattr(c, "text", None)
                if ctype in ("output_text", "text") and isinstance(ctext, str) and ctext.strip():
                    parts.append(ctext.strip())
        if parts:
            return "\n".join(parts).strip()

    return ""


def _openai_generate_text_any(prompt: str, system: str, model: str, temperature: float) -> str:
    """가능하면 Responses, 아니면 ChatCompletions로 텍스트 생성.

    - 키 권한이 Responses만/ChatCompletions만 허용된 경우 모두 대응
    - 실패 시 EXPORT_COMMENT_LAST_ERROR에 마지막 에러를 기록
    """
    client = get_openai_client()
    if not client:
        _set_export_comment_last_error("OPENAI_API_KEY 미설정 또는 클라이언트 초기화 실패")
        return ""

    # 1) Responses API 먼저 시도 (권한이 더 타이트한 키에서도 자주 허용)
    try:
        if hasattr(client, "responses") and hasattr(client.responses, "create"):
            # 시스템+유저 프롬프트를 한 문자열로 합쳐 전달 (SDK 버전 차이에 덜 민감)
            combined = (system or "").strip() + "\n\n" + (prompt or "").strip()
            resp = client.responses.create(
                model=model,
                input=combined,
                temperature=temperature,
            )
            txt = _extract_text_from_responses_obj(resp)
            if txt:
                return txt
    except Exception as e:
        _set_export_comment_last_error(f"Responses 실패: {e}")

    # 2) Chat Completions 시도
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system or ""},
                {"role": "user", "content": prompt or ""},
            ],
            temperature=temperature,
        )
        content = (resp.choices[0].message.content or "").strip()
        if content:
            return content
    except Exception as e:
        _set_export_comment_last_error(f"ChatCompletions 실패: {e}")

    return ""


def generate_export_comments(
    title: str,
    sport_label: str = "",
    count: int | None = None,
    mode: str = "simple",
    body_hint: str = "",
    avoid_text: str = "",
) -> str:
    """OpenAI로 '인간 댓글'을 생성해서 줄바꿈 문자열로 반환한다.

    mode:
      - "simple": 일반(심플) 게시글용 댓글
      - "deep":   심층 게시글용 댓글(더 디테일/심층 뉘앙스)

    ⚠️ 중요한 설계:
    - 실패해도 봇이 죽지 않도록 예외는 내부에서 처리
    - 실패 원인은 EXPORT_COMMENT_LAST_ERROR에 남김
    """
    enabled = (os.getenv("EXPORT_COMMENT_ENABLED", "1").strip().lower() not in ("0", "false", "no"))
    if not enabled:
        return ""

    mode = (mode or "simple").strip().lower()
    if mode not in ("simple", "deep"):
        mode = "simple"

    if count is None:
        try:
            count = int((os.getenv("EXPORT_COMMENT_COUNT", "") or "6").strip())
        except Exception:
            count = 6

    # 안전 범위
    count = max(1, min(int(count), 8))

    t = (title or "").strip()
    if not t:
        return ""

    # 모델: env 우선 + 안전 폴백
    primary_model = (os.getenv("EXPORT_COMMENT_MODEL") or os.getenv("SIMPLE_REWRITE_MODEL") or os.getenv("OPENAI_MODEL") or "").strip()
    model_candidates = [m for m in [primary_model, "gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"] if m]
    # 중복 제거
    seen = set()
    model_candidates = [m for m in model_candidates if (m not in seen and not seen.add(m))]

    try:
        temperature = float((os.getenv("EXPORT_COMMENT_TEMPERATURE", "0.95") or "0.95").strip())
    except Exception:
        temperature = 0.95
    temperature = max(0.2, min(1.2, temperature))

    parts = _parse_export_title_parts(t)
    league = parts.get("league", "")
    teams = parts.get("teams", "")
    date_s = parts.get("date", "")

    body_hint = (body_hint or "").strip()
    if body_hint:
        # 프롬프트 폭주 방지(토큰/비용보다 안정성 우선)
        if len(body_hint) > 1200:
            body_hint = body_hint[:1200].rstrip() + "…"

    avoid_text = (avoid_text or "").strip()
    if avoid_text and len(avoid_text) > 600:
        avoid_text = avoid_text[:600].rstrip() + "…"

    # 모드별 요구사항 차등
    extra_rules = ""
    if mode == "deep":
        extra_rules = (
            "- '심층' 또는 '디테일' 또는 '전술' 중 1개 표현을 자연스럽게 포함(각 댓글마다 꼭 동일 단어일 필요는 없음)\n"
            "- 너무 일반적인 칭찬만 하지 말고, 본문 힌트가 있으면 구체적인 포인트(예: 운영/압박/세트피스/매치업/로테이션 등)를 살짝 언급\n"
        )

    avoid_block = ""
    if avoid_text:
        avoid_block = f"\n중복 금지(아래 문장/표현을 그대로 따라하지 말 것):\n{avoid_text}\n"

    body_block = ""
    if mode == "deep" and body_hint:
        body_block = f"\n본문 힌트(일부):\n{body_hint}\n"

    prompt = f"""아래 제목을 바탕으로 네이버 카페에 달기 좋은 한국어 댓글을 {count}개 만들어줘.

제목:
{t}
{body_block}{avoid_block}
요구사항(중요):
- 출력은 댓글만. 번호/불릿/따옴표/머리말 금지.
- 댓글은 각 1줄, 줄바꿈으로 구분.
- 각 댓글은 서로 문장 구조/어투/길이가 겹치지 않게 다양하게.
- 제목에서 유추되는 '리그/대회명'과 '두 팀명'이 각 댓글에 반드시 들어가야 함.
- 과장 광고, 도박/베팅 유도, 링크, 해시태그, 이모지, "AI"라는 단어 금지.
- 너무 로봇처럼 반복하지 말고, 실제 사람이 분석글 읽고 남기는 자연스러운 톤.
{extra_rules}
참고 정보(있으면 반영):
- 날짜: {date_s or "(없음)"}
- 리그/대회: {league or "(없음)"}
- 팀 구간: {teams or "(없음)"}
- 종목 라벨: {sport_label or "(없음)"}
"""

    system = "You write natural Korean comments for sports community posts."

    # 모델 후보 순서대로 시도
    content = ""
    last_err = ""
    for m in (model_candidates or ["gpt-4o-mini"]):
        _set_export_comment_last_error("")
        content = _openai_generate_text_any(prompt=prompt, system=system, model=m, temperature=temperature)
        if content:
            break
        last_err = EXPORT_COMMENT_LAST_ERROR or last_err

    if not content:
        if last_err:
            print(f"[OPENAI][EXPORT_COMMENT] 생성 실패: {last_err}")
        return ""

    # 파싱/정리
    out_lines: list[str] = []
    for line in (content or "").splitlines():
        s = (line or "").strip()
        if not s:
            continue
        # 번호/불릿 제거
        s = re.sub(r"^\s*(?:[-•*]|\d+\s*[\).]|\d+\s*:)\s*", "", s).strip()
        if not s:
            continue
        # 너무 길면 살짝 컷(셀 가독성)
        if len(s) > 170:
            s = s[:170].rstrip() + "…"
        out_lines.append(s)

    # 중복 제거(순서 유지)
    seen2 = set()
    uniq: list[str] = []
    for s in out_lines:
        if s in seen2:
            continue
        seen2.add(s)
        uniq.append(s)

    if not uniq:
        return ""

    uniq = uniq[:count]
    return "\n".join(uniq).strip()


def generate_export_comments_pair(title: str, sport_label: str = "", body_hint: str = "", count: int | None = None) -> tuple[str, str]:
    """(comments, deep_comments) 쌍을 생성한다. deep 쪽은 simple 쪽과 중복을 피하도록 유도한다."""
    comments = generate_export_comments(title=title, sport_label=sport_label, count=count, mode="simple")
    deep_comments = generate_export_comments(title=title, sport_label=sport_label, count=count, mode="deep", body_hint=body_hint, avoid_text=comments)
    return (comments or "").strip(), (deep_comments or "").strip()

def append_export_rows(sheet_name: str, rows: list[list[str]]) -> bool:
    """지정 export 시트에 rows를 append.

    입력 row 포맷(호환):
      - 기본: [day, sport, src_id, title, body, createdAt]
      - 확장(구버전): [.., simple, comments, cafe_title, cafe_url, cafe_url_deep]
      - 확장(신버전): EXPORT_HEADER 길이만큼

    저장 포맷(항상 EXPORT_HEADER 순서):
      day, sport, src_id, title, body, createdAt, simple, cafe_title, cafe_url, comments, cafe_url_deep, deep_comments
    """
    if not rows:
        return True

    ws = get_export_ws(sheet_name)
    if not ws:
        return False

    # 컬럼 인덱스(헤더명 기반)
    def _h(name: str, fallback: int) -> int:
        try:
            return EXPORT_HEADER.index(name)
        except ValueError:
            return fallback

    i_day = _h("day", 0)
    i_sport = _h("sport", 1)
    i_src = _h("src_id", 2)
    i_title = _h("title", 3)
    i_body = _h("body", 4)
    i_created = _h("createdAt", 5)
    i_simple = _h("simple", 6)
    i_cafe_title = _h("cafe_title", 7)
    i_cafe_url = _h("cafe_url", 8)
    i_comments = _h("comments", 9)
    i_cafe_url_deep = _h("cafe_url_deep", 10)
    i_deep_comments = _h("deep_comments", 11)

    fixed_rows: list[list[str]] = []
    for r in rows:
        if not r:
            continue
        legacy = list(r)

        # 최소 6컬럼 맞춤(기본 필드)
        while len(legacy) < 6:
            legacy.append("")

        # 신버전(이미 EXPORT_HEADER 길이 이상)이면 우선 그대로 사용
        if len(legacy) >= len(EXPORT_HEADER):
            rr = legacy[: len(EXPORT_HEADER)]
        else:
            # 구버전/기본 포맷 → 신 헤더로 매핑
            rr = [""] * len(EXPORT_HEADER)
            rr[i_day] = legacy[0]
            rr[i_sport] = legacy[1]
            rr[i_src] = legacy[2]
            rr[i_title] = legacy[3]
            rr[i_body] = legacy[4]
            rr[i_created] = legacy[5]

            # simple(구버전: index 6)
            if len(legacy) > 6:
                rr[i_simple] = legacy[6]

            # comments/cafe_*(구버전 헤더: simple 다음에 comments, cafe_title, cafe_url, cafe_url_deep)
            if len(legacy) > 7:
                rr[i_comments] = legacy[7]
            if len(legacy) > 8:
                rr[i_cafe_title] = legacy[8]
            if len(legacy) > 9:
                rr[i_cafe_url] = legacy[9]
            if len(legacy) > 10:
                rr[i_cafe_url_deep] = legacy[10]

        day = rr[i_day] if i_day < len(rr) else ""
        sport_label = rr[i_sport] if i_sport < len(rr) else ""
        title = rr[i_title] if i_title < len(rr) else ""
        body = rr[i_body] if i_body < len(rr) else ""

        # simple 보정(비어있으면 body에서 추출)
        if i_simple < len(rr) and not str(rr[i_simple]).strip():
            rr[i_simple] = extract_simple_from_body(body)

        # base_title: 우선 title, 없으면 simple 첫 줄
        base_title = (title or "").strip()
        if not base_title:
            simple_txt = (rr[i_simple] if i_simple < len(rr) else "") or ""
            base_title = (simple_txt.splitlines()[0] if simple_txt else "").strip()

        # comments(심플용) 생성/보정
        if i_comments < len(rr) and (not str(rr[i_comments]).strip()):
            comments, deep_comments = generate_export_comments_pair(
                title=base_title,
                sport_label=str(sport_label or "").strip(),
                body_hint=str(body or "").strip(),
            )
            rr[i_comments] = (comments or "").strip()
            # deep_comments도 동시에 채워주되, 이미 값이 있으면 유지
            if i_deep_comments < len(rr) and (not str(rr[i_deep_comments]).strip()):
                rr[i_deep_comments] = (deep_comments or "").strip()
        else:
            # comments가 이미 있고 deep_comments만 비어있으면 deep만 생성
            if i_deep_comments < len(rr) and (not str(rr[i_deep_comments]).strip()):
                deep_comments = generate_export_comments(
                    title=base_title,
                    sport_label=str(sport_label or "").strip(),
                    mode="deep",
                    body_hint=str(body or "").strip(),
                    avoid_text=str(rr[i_comments] if i_comments < len(rr) else ""),
                )
                rr[i_deep_comments] = (deep_comments or "").strip()

        # rr 길이 보정
        if len(rr) < len(EXPORT_HEADER):
            rr += [""] * (len(EXPORT_HEADER) - len(rr))
        if len(rr) > len(EXPORT_HEADER):
            rr = rr[: len(EXPORT_HEADER)]

        fixed_rows.append(rr)

    if not fixed_rows:
        return True

    try:
        ws.append_rows(fixed_rows, value_input_option="RAW", table_range="A1")
        return True
    except Exception as e:
        print(f"[GSHEET][EXPORT] append 실패({sheet_name}): {e}")
        return False

# ───────────────── Export 댓글 채우기(OpenAI) ─────────────────

async def export_comment_fill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """기존 export 시트의 comments / deep_comments 컬럼을 채운다.

    사용:
      /export_comment_fill
      /export_comment_fill today|tomorrow|all [limit] [force] [deep|both]

    기본 동작:
      - tomorrow 시트에서 comments(심플용)가 비어있는 행을 최신순으로 최대 30개 채움

    옵션:
      - deep : deep_comments(심층용)만 채움
      - both : comments + deep_comments를 함께 채움(비어있는 것만)
      - force: 이미 값이 있어도 덮어쓰기
    """
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    args = [a.strip() for a in (context.args or []) if a and a.strip()]
    target = "tomorrow"
    limit = 30
    force = False
    mode = "simple"  # simple | deep | both

    for a in args:
        al = a.lower()
        if al in ("today", "tomorrow", "all"):
            target = al
        elif al.isdigit():
            try:
                limit = int(al)
            except Exception:
                pass
        elif al in ("force", "overwrite"):
            force = True
        elif al in ("deep", "deep_comments", "deepcomment", "deepcomments"):
            mode = "deep"
        elif al in ("both", "allcols", "allcol", "bothcols"):
            mode = "both"

    limit = max(1, min(int(limit), 200))

    if target == "all":
        sheet_names = [EXPORT_TODAY_SHEET_NAME, EXPORT_TOMORROW_SHEET_NAME]
    elif target == "today":
        sheet_names = [EXPORT_TODAY_SHEET_NAME]
    else:
        sheet_names = [EXPORT_TOMORROW_SHEET_NAME]

    # OpenAI 설정 체크
    if os.getenv("EXPORT_COMMENT_ENABLED", "1").strip().lower() in ("0", "false", "no"):
        await update.message.reply_text("EXPORT_COMMENT_ENABLED=0 상태라 댓글 생성이 비활성화되어 있습니다.")
        return

    total_updated_simple = 0
    total_updated_deep = 0

    for sheet_name in sheet_names:
        ws = get_export_ws(sheet_name)
        if not ws:
            await update.message.reply_text(f"{sheet_name} 시트를 찾을 수 없습니다.")
            continue

        try:
            vals = ws.get_all_values()
        except Exception as e:
            await update.message.reply_text(f"{sheet_name} 읽기 실패: {e}")
            continue

        if not vals or len(vals) <= 1:
            continue

        header = vals[0]

        def _idx(name: str, fallback: int) -> int:
            try:
                return header.index(name)
            except ValueError:
                return fallback

        i_sport = _idx("sport", 1)
        i_title = _idx("title", 3)
        i_body = _idx("body", 4)
        i_simple = _idx("simple", 6)
        i_comments = _idx("comments", 9)
        i_deep = _idx("deep_comments", 11)

        # deep_comments 컬럼이 없는 상태면 알려주기
        if mode in ("deep", "both") and ("deep_comments" not in header):
            await update.message.reply_text(f"{sheet_name} 시트에 deep_comments 컬럼이 없습니다. 헤더 보정이 필요합니다.")
            # 계속 진행(헤더 재배치가 실패했거나 수동 수정 중일 수 있음)

        updates: list[dict] = []
        updated_simple = 0
        updated_deep = 0

        # 최신순(아래쪽)부터 채우기
        for row_idx, r in enumerate(reversed(vals[1:]), start=2):
            # reversed에서 row_idx 계산은 실제 행번호와 다르므로 재계산
            real_row_idx = len(vals) - (row_idx - 2)

            sportv = (r[i_sport] if len(r) > i_sport else "").strip()
            titlev = (r[i_title] if len(r) > i_title else "").strip()
            bodyv = (r[i_body] if len(r) > i_body else "").strip()
            simplev = (r[i_simple] if len(r) > i_simple else "").strip()
            comments_raw = (r[i_comments] if len(r) > i_comments else "").strip() if i_comments >= 0 else ""
            deep_raw = (r[i_deep] if len(r) > i_deep else "").strip() if i_deep >= 0 else ""

            base_title = titlev or (simplev.splitlines()[0].strip() if simplev else "")
            if not base_title:
                continue

            need_simple = (mode in ("simple", "both")) and (force or not comments_raw)
            need_deep = (mode in ("deep", "both")) and (force or not deep_raw)

            if not (need_simple or need_deep):
                continue

            # limit 적용: "생성 작업 수" 기준(경기 기준)
            if (updated_simple + updated_deep) >= limit:
                break

            # 생성
            new_comments = comments_raw
            new_deep = deep_raw

            try:
                if need_simple and need_deep and (not comments_raw) and (not deep_raw):
                    # 둘 다 비어있으면 pair 생성(중복 회피 유도)
                    new_comments, new_deep = generate_export_comments_pair(
                        title=base_title,
                        sport_label=sportv,
                        body_hint=bodyv,
                    )
                else:
                    if need_simple:
                        new_comments = generate_export_comments(
                            title=base_title,
                            sport_label=sportv,
                            mode="simple",
                        )
                    if need_deep:
                        new_deep = generate_export_comments(
                            title=base_title,
                            sport_label=sportv,
                            mode="deep",
                            body_hint=bodyv,
                            avoid_text=new_comments if new_comments else comments_raw,
                        )
            except Exception as e:
                print(f"[OPENAI][EXPORT_COMMENT] 생성 예외: {e}")
                continue

            # 업데이트 예약
            if need_simple and i_comments >= 0:
                col = _col_letter(i_comments + 1)
                updates.append({"range": f"{col}{real_row_idx}", "values": [[(new_comments or "").strip()]]})
                updated_simple += 1

            if need_deep and i_deep >= 0:
                col = _col_letter(i_deep + 1)
                updates.append({"range": f"{col}{real_row_idx}", "values": [[(new_deep or "").strip()]]})
                updated_deep += 1

        if updates:
            try:
                ws.batch_update(updates, value_input_option="RAW")
            except Exception as e:
                # batch_update 실패 시 단건 update로 폴백
                print(f"[GSHEET][EXPORT] batch_update 실패 → 폴백: {e}")
                for u in updates:
                    try:
                        ws.update(range_name=u["range"], values=u["values"])
                    except Exception as e2:
                        print(f"[GSHEET][EXPORT] 단건 update 실패({sheet_name} {u.get('range')}): {e2}")

        total_updated_simple += updated_simple
        total_updated_deep += updated_deep

    msg = "✅ export 댓글 채우기 완료\n"
    msg += f"- comments(심플): {total_updated_simple}개\n"
    msg += f"- deep_comments(심층): {total_updated_deep}개\n"
    await update.message.reply_text(msg)

def _parse_export_comment_txt_args(args: list[str]) -> tuple[str, int, str]:
    """TXT 생성 옵션 파싱.
    사용 예)
      /export_comment_txt                -> tomorrow, 10경기, 전체
      /export_comment_txt tomorrow       -> tomorrow, 10경기, 전체
      /export_comment_txt today 5 soccer -> today, 5경기, soccer
    """
    which = "tomorrow"
    limit_matches = int(os.getenv("EXPORT_COMMENT_TXT_MATCHES", "10"))
    sport_filter = ""

    for a in (args or []):
        a = (a or "").strip().lower()
        if not a:
            continue
        if a in ("today", "tomorrow"):
            which = a
            continue
        if a.isdigit():
            limit_matches = max(1, min(50, int(a)))
            continue
        if a in ("soccer", "baseball", "basketball", "volleyball"):
            sport_filter = a
            continue

    return which, limit_matches, sport_filter


def _split_comment_lines(s: str) -> list[str]:
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [x.strip() for x in s.split("\n")]
    return [x for x in lines if x]


def _build_export_comment_txt_markup(which: str, sport_filter: str, limit_matches: int | None = None) -> InlineKeyboardMarkup:
    n = limit_matches or int(os.getenv("EXPORT_COMMENT_TXT_MATCHES", "10"))
    cb = f"txt:{which}:{sport_filter}:{n}"
    label = f"📄 댓글 TXT 받기 ({n}경기)"
    if sport_filter:
        label = f"📄 {sport_filter} 댓글 TXT ({n}경기)"
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=cb)]])


def _build_export_comment_txt_markup_bv(which: str, limit_matches: int | None = None) -> InlineKeyboardMarkup:
    n = limit_matches or int(os.getenv("EXPORT_COMMENT_TXT_MATCHES", "10"))
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"📄 basketball TXT ({n}경기)", callback_data=f"txt:{which}:basketball:{n}"),
            InlineKeyboardButton(f"📄 volleyball TXT ({n}경기)", callback_data=f"txt:{which}:volleyball:{n}"),
        ]
    ])


async def _send_export_comment_txt_files(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    which: str,
    limit_matches: int,
    sport_filter: str = "",
) -> tuple[int, int]:
    """export_* 시트의 comments(H) 줄바꿈을 TXT 파일들로 전송한다.
    반환: (전송한 파일 수, 처리한 경기 수)
    """
    which = (which or "tomorrow").strip().lower()
    if which not in ("today", "tomorrow"):
        which = "tomorrow"

    max_files = int(os.getenv("EXPORT_COMMENT_TXT_MAX_FILES", "120"))
    delay = float(os.getenv("EXPORT_COMMENT_TXT_SEND_DELAY_SEC", "0.12"))

    sheet_name = EXPORT_TODAY_SHEET_NAME if which == "today" else EXPORT_TOMORROW_SHEET_NAME
    ws = get_export_ws(sheet_name)
    if not ws:
        await context.bot.send_message(chat_id=chat_id, text=f"{sheet_name} 시트를 못 찾았어.")
        return 0, 0

    vals = ws.get_all_values()
    if not vals or len(vals) <= 1:
        await context.bot.send_message(chat_id=chat_id, text=f"{sheet_name}에 데이터가 없어.")
        return 0, 0

    header = vals[0]

    def _idx(name: str, fallback: int) -> int:
        try:
            return header.index(name)
        except ValueError:
            return fallback

    i_sport = _idx("sport", 1)
    i_src = _idx("src_id", 2)
    i_title = _idx("title", 3)
    i_comments = _idx("comments", 7)

    # 최신 행부터 역순으로 N경기 선별
    selected: list[tuple[str, str, list[str]]] = []  # (sid, title, comment_lines)
    for r in reversed(vals[1:]):
        sportv = (r[i_sport] if len(r) > i_sport else "").strip()
        if sport_filter and (not _cafe_sport_match(sportv, sport_filter)):
            continue

        sid = (r[i_src] if len(r) > i_src else "").strip()
        title = (r[i_title] if len(r) > i_title else "").strip()
        comments_raw = (r[i_comments] if len(r) > i_comments else "")
        comment_lines = _split_comment_lines(comments_raw)

        # comments가 비어있으면 넘어감
        if not comment_lines:
            continue

        selected.append((sid, title, comment_lines))
        if len(selected) >= limit_matches:
            break

    if not selected:
        msg = "TXT로 보낼 댓글이 없어. export 시트 H열(comments)이 비어있는지 확인해줘."
        if sport_filter:
            msg = f"TXT로 보낼 댓글이 없어({sport_filter}). export 시트 H열(comments)을 먼저 채워줘."
        await context.bot.send_message(chat_id=chat_id, text=msg)
        return 0, 0

    total_files = 0
    total_matches = len(selected)

    # 너무 많이 보내면 운영이 힘들어서 제한
    est_files = sum(len(x[2]) for x in selected)
    if est_files > max_files:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"댓글 파일이 너무 많아서 {max_files}개까지만 보낼게. (예상 {est_files}개)",
        )

    for (sid, title, lines_) in selected:
        for idx, text in enumerate(lines_, start=1):
            if total_files >= max_files:
                break
            # 파일명(너무 길면 문제되니 src_id 위주)
            safe_sid = re.sub(r"[^0-9A-Za-z_\-]+", "_", sid) or "comment"
            filename = f"{safe_sid}_{idx:02d}.txt"

            bio = io.BytesIO(text.encode("utf-8"))
            doc = InputFile(bio, filename=filename)
            try:
                await context.bot.send_document(chat_id=chat_id, document=doc)
                total_files += 1
            except Exception as e:
                print(f"[EXPORT][TXT] send_document 실패({filename}): {e}")
                continue

            # 텔레그램 rate-limit 완화
            if delay > 0:
                await asyncio.sleep(delay)

        if total_files >= max_files:
            break

    return total_files, total_matches


def _safe_zip_basename(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return "item"
    s = re.sub(r"[^0-9A-Za-z_\-]+", "_", s)
    s = s.strip("_")
    return s[:60] if s else "item"


def _default_zip_matches() -> int:
    # zip 기본 경기수: EXPORT_COMMENT_ZIP_MATCHES > EXPORT_COMMENT_TXT_MATCHES > 10
    try:
        return int(os.getenv("EXPORT_COMMENT_ZIP_MATCHES") or os.getenv("EXPORT_COMMENT_TXT_MATCHES") or "10")
    except Exception:
        return 10


def _build_export_comment_zip_markup(which: str, sport_filter: str, limit_matches: int | None = None) -> InlineKeyboardMarkup:
    n = limit_matches or _default_zip_matches()
    cb = f"zip:{which}:{sport_filter}:{n}"
    label = f"📦 댓글 ZIP 받기 ({n}경기)"
    if sport_filter:
        label = f"📦 {sport_filter} ZIP ({n}경기)"
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=cb)]])


def _build_export_comment_zip_markup_bv(which: str, limit_matches: int | None = None) -> InlineKeyboardMarkup:
    n = limit_matches or _default_zip_matches()
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"📦 basketball ZIP ({n}경기)", callback_data=f"zip:{which}:basketball:{n}"),
            InlineKeyboardButton(f"📦 volleyball ZIP ({n}경기)", callback_data=f"zip:{which}:volleyball:{n}"),
        ]
    ])


def _build_export_comment_zip_markup_all(which: str, limit_matches: int | None = None) -> InlineKeyboardMarkup:
    # 4종목 버튼 한번에
    n = limit_matches or _default_zip_matches()
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"📦 soccer ZIP ({n})", callback_data=f"zip:{which}:soccer:{n}"),
            InlineKeyboardButton(f"📦 baseball ZIP ({n})", callback_data=f"zip:{which}:baseball:{n}"),
        ],
        [
            InlineKeyboardButton(f"📦 basketball ZIP ({n})", callback_data=f"zip:{which}:basketball:{n}"),
            InlineKeyboardButton(f"📦 volleyball ZIP ({n})", callback_data=f"zip:{which}:volleyball:{n}"),
        ],
    ])


async def _send_export_comment_zip_file(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    which: str,
    limit_matches: int,
    sport_filter: str = "",
) -> tuple[int, int, str]:
    """export_* 시트의 comments(H) 줄바꿈을 ZIP(내부: 한 줄당 txt 1개)로 묶어 전송.
    반환: (zip에 담긴 txt 파일 수, 처리한 경기 수, zip 파일명)
    """
    which = (which or "tomorrow").strip().lower()
    if which not in ("today", "tomorrow"):
        which = "tomorrow"

    max_files = int(os.getenv("EXPORT_COMMENT_ZIP_MAX_FILES", os.getenv("EXPORT_COMMENT_TXT_MAX_FILES", "600")))

    sheet_name = EXPORT_TODAY_SHEET_NAME if which == "today" else EXPORT_TOMORROW_SHEET_NAME
    ws = get_export_ws(sheet_name)
    if not ws:
        await context.bot.send_message(chat_id=chat_id, text=f"{sheet_name} 시트를 못 찾았어.")
        return 0, 0, ""

    vals = ws.get_all_values()
    if not vals or len(vals) <= 1:
        await context.bot.send_message(chat_id=chat_id, text=f"{sheet_name}에 데이터가 없어.")
        return 0, 0, ""

    header = vals[0]

    def _idx(name: str, fallback: int) -> int:
        try:
            return header.index(name)
        except ValueError:
            return fallback

    i_sport = _idx("sport", 1)
    i_src = _idx("src_id", 2)
    i_title = _idx("title", 3)
    i_comments = _idx("comments", 7)

    selected: list[tuple[str, str, list[str]]] = []  # (sid, title, comment_lines)
    for r in reversed(vals[1:]):
        sportv = (r[i_sport] if len(r) > i_sport else "").strip()
        if sport_filter and (not _cafe_sport_match(sportv, sport_filter)):
            continue

        sid = (r[i_src] if len(r) > i_src else "").strip()
        title = (r[i_title] if len(r) > i_title else "").strip()
        comments_raw = (r[i_comments] if len(r) > i_comments else "")
        comment_lines = _split_comment_lines(comments_raw)

        if not comment_lines:
            continue

        selected.append((sid, title, comment_lines))
        if len(selected) >= limit_matches:
            break

    if not selected:
        msg = "ZIP으로 보낼 댓글이 없어. export 시트 H열(comments)을 먼저 채워줘."
        if sport_filter:
            msg = f"ZIP으로 보낼 댓글이 없어({sport_filter}). export 시트 H열(comments)을 먼저 채워줘."
        await context.bot.send_message(chat_id=chat_id, text=msg)
        return 0, 0, ""

    ts = now_kst().strftime("%Y%m%d_%H%M%S")
    sport_tag = sport_filter or "all"
    zip_filename = f"comments_{which}_{sport_tag}_{ts}.zip"

    bio = io.BytesIO()
    total_files = 0
    total_matches = 0

    try:
        with zipfile.ZipFile(bio, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for (sid, title, comment_lines) in selected:
                base = _safe_zip_basename(sid) if sid else _safe_zip_basename(title) or "item"
                total_matches += 1
                for idx, line in enumerate(comment_lines, start=1):
                    if total_files >= max_files:
                        break
                    fname = f"{base}_{idx:02d}.txt"
                    zf.writestr(fname, (line.strip() + "\n").encode("utf-8"))
                    total_files += 1
                if total_files >= max_files:
                    break

        bio.seek(0)
        doc = InputFile(bio, filename=zip_filename)
        await context.bot.send_document(chat_id=chat_id, document=doc)
        return total_files, total_matches, zip_filename

    except Exception as e:
        print(f"[EXPORT][ZIP] zip 생성/전송 실패: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"ZIP 생성/전송 중 오류: {e}")
        return 0, 0, ""


async def export_comment_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """export 시트 H열(comments) → 한 줄당 txt를 ZIP으로 묶어 전송.
    - /export_comment_zip [today|tomorrow] [경기수] [soccer|baseball|basketball|volleyball]
    """
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    which, limit_matches, sport_filter = _parse_export_comment_txt_args(getattr(context, "args", []) or [])
    await update.message.reply_text(f"📦 댓글 ZIP 생성/전송 시작: {which}, {limit_matches}경기, sport={sport_filter or 'ALL'}")

    files_cnt, matches_cnt, zip_name = await _send_export_comment_zip_file(
        chat_id=update.effective_chat.id,
        context=context,
        which=which,
        limit_matches=limit_matches,
        sport_filter=sport_filter,
    )

    if files_cnt and matches_cnt:
        await update.message.reply_text(f"✅ ZIP 전송 완료: {matches_cnt}경기 / {files_cnt}개 파일 (1 zip: {zip_name})")
    else:
        await update.message.reply_text("ZIP 전송할 댓글이 없거나 실패했습니다.")


async def export_comment_zip_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """댓글 ZIP 받기 버튼을 다시 소환하는 명령어.
    사용 예)
      /export_comment_zip_buttons
      /export_comment_zip_buttons tomorrow 10 soccer
      /export_comment_zip_buttons today 30
      /export_comment_zip_buttons tomorrow 50 (전체 종목 버튼)
    """
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    which, limit_matches, sport_filter = _parse_export_comment_txt_args(getattr(context, "args", []) or [])
    which = which or "tomorrow"
    limit_matches = limit_matches or _default_zip_matches()

    if not sport_filter:
        markup = _build_export_comment_zip_markup_all(which, limit_matches)
        await update.message.reply_text(
            f"📦 댓글 ZIP 받기 버튼입니다: {which}, {limit_matches}경기 (종목 선택)",
            reply_markup=markup,
        )
        return

    if sport_filter in ("bv", "basketvolley", "basket_volley"):
        markup = _build_export_comment_zip_markup_bv(which, limit_matches)
        await update.message.reply_text(
            f"📦 댓글 ZIP 받기 버튼입니다: {which}, {limit_matches}경기 (농구/배구)",
            reply_markup=markup,
        )
        return

    markup = _build_export_comment_zip_markup(which, sport_filter, limit_matches)
    await update.message.reply_text(
        f"📦 댓글 ZIP 받기 버튼입니다: {which}, {limit_matches}경기, sport={sport_filter}",
        reply_markup=markup,
    )

async def export_comment_txt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """export 시트 H열(comments) → 한 줄당 1개 TXT 파일로 보내기.
    - /export_comment_txt [today|tomorrow] [경기수] [soccer|baseball|basketball|volleyball]
    """
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    which, limit_matches, sport_filter = _parse_export_comment_txt_args(getattr(context, "args", []) or [])
    await update.message.reply_text(f"📄 댓글 TXT 생성/전송 시작: {which}, {limit_matches}경기, sport={sport_filter or 'ALL'}")

    sent_files, processed = await _send_export_comment_txt_files(
        chat_id=update.effective_chat.id,
        context=context,
        which=which,
        limit_matches=limit_matches,
        sport_filter=sport_filter,
    )

    await update.message.reply_text(f"✅ TXT 전송 완료: {processed}경기 / {sent_files}개 파일")




# ───────────────── 메뉴(인라인/리플라이) 구성 ─────────────────

def build_reply_keyboard() -> ReplyKeyboardMarkup:
    """DM에서 쓸 간단 리플라이 키보드."""
    keyboard = [
        ["메뉴 미리보기", "도움말"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def build_main_inline_menu() -> InlineKeyboardMarkup:
    """채널/DM 공통 메인 인라인 메뉴."""
    today_str, tomorrow_str = get_date_labels()
    buttons = [
        [InlineKeyboardButton("📺 실시간 무료 중계", url="https://goat-tv.com")],
        [InlineKeyboardButton(f"📌 {today_str} 경기 분석픽", callback_data="analysis_root:today")],
        [InlineKeyboardButton(f"📌 {tomorrow_str} 경기 분석픽", callback_data="analysis_root:tomorrow")],
        [InlineKeyboardButton("📰 스포츠 뉴스 요약", callback_data="news_root")],
    ]
    return InlineKeyboardMarkup(buttons)


def _ordered_analysis_categories(keys: list[str]) -> list[str]:
    # 보기 편한 순서 우선 배치
    priority = [
        "해외축구", "K리그", "J리그",
        "KBO", "NPB", "해외야구",
        "NBA", "KBL",
        "V리그",
        "축구", "야구", "농구", "배구",
    ]
    def ksort(k: str):
        if k in priority:
            return (0, priority.index(k))
        return (1, k)
    return sorted(keys, key=ksort)


def build_analysis_category_menu(key: str) -> InlineKeyboardMarkup:
    """today/tomorrow 분석 종목(카테고리) 선택 메뉴."""
    data = ANALYSIS_DATA_MAP.get(key, {}) or {}
    keys = list(data.keys())
    ordered = _ordered_analysis_categories(keys)

    buttons: list[list[InlineKeyboardButton]] = []
    for sport in ordered:
        cnt = len(data.get(sport, []) or [])
        label = f"{sport} ({cnt})" if cnt else sport
        buttons.append([InlineKeyboardButton(label, callback_data=f"analysis_cat:{key}:{sport}")])

    buttons.append([InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)


def build_analysis_match_menu(key: str, sport: str, page: int = 1, per_page: int = 10) -> InlineKeyboardMarkup:
    """특정 종목의 경기 리스트(페이지네이션) 메뉴."""
    items = (ANALYSIS_DATA_MAP.get(key, {}) or {}).get(sport, []) or []
    total = len(items)

    if total <= 0:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("데이터 없음 (크롤링/시트 확인)", callback_data="noop")],
            [InlineKeyboardButton("◀ 종목 선택으로", callback_data=f"analysis_root:{key}")],
            [InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")],
        ])

    max_page = max(1, math.ceil(total / per_page))
    page = max(1, min(page, max_page))
    start = (page - 1) * per_page
    end = start + per_page

    page_items = items[start:end]

    buttons: list[list[InlineKeyboardButton]] = []
    for it in page_items:
        title = (it.get("title") or "").strip() or "경기"
        if len(title) > 36:
            title = title[:36] + "…"

        match_id = (it.get("id") or "").strip()
        buttons.append([InlineKeyboardButton(title, callback_data=f"match:{key}:{sport}:{match_id}")])

    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅ 이전", callback_data=f"match_page:{key}:{sport}:{page-1}"))
    if page < max_page:
        nav.append(InlineKeyboardButton("다음 ➡", callback_data=f"match_page:{key}:{sport}:{page+1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("◀ 종목 선택으로", callback_data=f"analysis_root:{key}")])
    buttons.append([InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")])

    return InlineKeyboardMarkup(buttons)


def build_news_category_menu() -> InlineKeyboardMarkup:
    """뉴스 종목 선택 메뉴."""
    data = NEWS_DATA or {}
    base_order = ["축구", "야구", "농구", "배구"]
    keys = list(dict.fromkeys(base_order + sorted([k for k in data.keys() if k not in base_order])))

    buttons: list[list[InlineKeyboardButton]] = []
    for sport in keys:
        cnt = len(data.get(sport, []) or [])
        label = f"{sport} ({cnt})" if cnt else sport
        buttons.append([InlineKeyboardButton(label, callback_data=f"news_cat:{sport}")])

    buttons.append([InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)


def build_news_list_menu(sport: str, per_page: int = 10) -> InlineKeyboardMarkup:
    """특정 종목 뉴스 리스트(최대 per_page개)."""
    items = (NEWS_DATA or {}).get(sport, []) or []
    if not items:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("데이터 없음 (뉴스 크롤링 필요)", callback_data="noop")],
            [InlineKeyboardButton("◀ 다른 종목", callback_data="news_root")],
            [InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")],
        ])

    buttons: list[list[InlineKeyboardButton]] = []
    for it in items[:per_page]:
        title = (it.get("title") or "").strip() or "뉴스"
        if len(title) > 30:
            title = title[:30] + "…"
        nid = (it.get("id") or "").strip()
        buttons.append([InlineKeyboardButton(title, callback_data=f"news_item:{sport}:{nid}")])

    buttons.append([InlineKeyboardButton("◀ 다른 종목", callback_data="news_root")])
    buttons.append([InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)

async def send_main_menu(chat_id: int | str, context: ContextTypes.DEFAULT_TYPE, preview: bool = False):
    """
    채널/DM 공통으로 '텍스트 + 메인 메뉴 버튼' 전송.
    """
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=get_menu_caption(),
        reply_markup=build_main_inline_menu(),
    )
    return msg


# ───────────────── 핸들러들 ─────────────────

# 1) /start – DM에서 채널과 동일한 레이아웃 or 바로 메뉴 진입
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = context.args
    mode = args[0] if args else None

    today_str, tomorrow_str = get_date_labels()

    if mode == "today":
        await update.message.reply_text(
            f"{today_str} 경기 분석픽 메뉴입니다. 종목을 선택하세요 👇",
            reply_markup=build_analysis_category_menu("today"),
        )
        return

    if mode == "tomorrow":
        await update.message.reply_text(
            f"{tomorrow_str} 경기 분석픽 메뉴입니다. 종목을 선택하세요 👇",
            reply_markup=build_analysis_category_menu("tomorrow"),
        )
        return

    if mode == "news":
        await update.message.reply_text(
            "스포츠 뉴스 요약입니다. 종목을 선택하세요 👇",
            reply_markup=build_news_category_menu(),
        )
        return

    await update.message.reply_text(
        "스포츠봇입니다.\n"
        "아래에는 채널에 올라갈 메뉴와 동일한 레이아웃 미리보기를 보여줄게.\n"
        "실제 채널 배포는 /publish 명령으로 진행하면 돼.",
        reply_markup=build_reply_keyboard(),
    )

    await send_main_menu(chat_id, context, preview=True)


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(f"당신의 텔레그램 ID: {uid}")


# 2) DM 텍스트 처리 – 간단 테스트용
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if "메뉴 미리보기" in text:
        await start(update, context)
    elif "도움말" in text:
        await update.message.reply_text(
            "/start : 메뉴 미리보기\n"
            "/publish : 채널에 메뉴 전송 + 상단 고정"
        )
    else:
        await update.message.reply_text("메뉴 미리보기는 /start 또는 '메뉴 미리보기' 버튼을 눌러주세요.")


# 3) /publish – 채널로 메뉴 보내고 상단 고정
async def publish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    if not CHANNEL_ID:
        await update.message.reply_text("CHANNEL_ID가 비어 있습니다. Render 환경변수에 CHANNEL_ID를 설정하세요.")
        return

    try:
        await context.bot.unpin_all_chat_messages(CHANNEL_ID)
    except Exception:
        pass

    msg = await send_main_menu(CHANNEL_ID, context, preview=False)

    await context.bot.pin_chat_message(
        chat_id=CHANNEL_ID,
        message_id=msg.message_id,
        disable_notification=True,
    )

    await update.message.reply_text("채널에 메뉴를 올리고 상단에 고정했습니다 ✅")


# 5) /syncsheet – 구글시트에서 분석/뉴스 데이터 다시 로딩
async def syncsheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    try:
        reload_analysis_from_sheet()
        reload_news_from_sheet()
        await update.message.reply_text("구글시트에서 분석 데이터를 다시 불러왔습니다 ✅")
    except Exception as e:
        await update.message.reply_text(f"구글시트 로딩 중 오류가 발생했습니다: {e}")


# 🔹 /newsclean – news 시트 초기화 (헤더만 남기기)
async def newsclean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")

    if not (client_gs and spreadsheet_id):
        await update.message.reply_text(
            "구글시트 설정(GOOGLE_SERVICE_KEY 또는 SPREADSHEET_ID)이 없어 시트를 초기화할 수 없습니다."
        )
        return

    try:
        sh = client_gs.open_by_key(spreadsheet_id)
        ws = sh.worksheet(os.getenv("SHEET_NEWS_NAME", "news"))
    except Exception as e:
        await update.message.reply_text(f"뉴스 시트를 열지 못했습니다: {e}")
        return

    try:
        rows = ws.get_all_values()
        if rows:
            header = rows[0]
        else:
            header = ["sport", "id", "title", "summary"]

        ws.clear()
        ws.update(range_name="A1", values=[header])

        await update.message.reply_text("뉴스 시트를 초기화했습니다. (헤더만 남겨둠) ✅")

    except Exception as e:
        await update.message.reply_text(f"시트 초기화 중 오류: {e}")
        return

# 🔹 /allclean – today / tomorrow / news 시트 전체 초기화
async def allclean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """today/tomorrow/news + export_today/export_tomorrow 시트를 모두 초기화(헤더만 유지)."""
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")

    if not (client_gs and spreadsheet_id):
        await update.message.reply_text(
            "구글시트 설정(GOOGLE_SERVICE_KEY 또는 SPREADSHEET_ID)이 없어 시트를 초기화할 수 없습니다."
        )
        return

    try:
        sh = client_gs.open_by_key(spreadsheet_id)
    except Exception as e:
        await update.message.reply_text(f"스프레드시트를 열지 못했습니다: {e}")
        return

    sheet_today_name = os.getenv("SHEET_TODAY_NAME", "today")
    sheet_tomorrow_name = os.getenv("SHEET_TOMORROW_NAME", "tomorrow")
    sheet_news_name = os.getenv("SHEET_NEWS_NAME", "news")

    # 분석/뉴스 헤더(로드 함수 기준)
    analysis_header = ["sport", "id", "title", "summary"]
    export_header = EXPORT_HEADER  # ["day","sport","src_id","title","body","createdAt"]

    sheet_configs = [
        (sheet_today_name, "today 분석", analysis_header),
        (sheet_tomorrow_name, "tomorrow 분석", analysis_header),
        (sheet_news_name, "news 뉴스", analysis_header),
        (EXPORT_TODAY_SHEET_NAME, "export_today", export_header),
        (EXPORT_TOMORROW_SHEET_NAME, "export_tomorrow", export_header),
    ]

    errors: list[str] = []

    for sheet_name, desc, header in sheet_configs:
        try:
            ws = _get_ws_by_name(sh, sheet_name)
            if not ws:
                ws = sh.add_worksheet(title=sheet_name, rows=2000, cols=max(10, len(header)))
        except Exception as e:
            errors.append(f"{desc} 시트를 열지 못했습니다: {e}")
            continue

        try:
            ws.clear()
            ws.update(range_name="A1", values=[header])
        except Exception as e:
            errors.append(f"{desc} 초기화 실패: {e}")

    # 메모리 데이터도 함께 리셋(분석/뉴스)
    try:
        reload_analysis_from_sheet()
    except Exception as e:
        errors.append(f"메모리(analysis) 리셋 실패: {e}")

    try:
        reload_news_from_sheet()
    except Exception as e:
        errors.append(f"메모리(news) 리셋 실패: {e}")

    if errors:
        msg = "일부 시트를 초기화하지 못했습니다.\n\n" + "\n".join(errors)
    else:
        msg = "today / tomorrow / news / export_today / export_tomorrow 시트를 모두 초기화했습니다. (헤더만 남겨둠) ✅"

    await update.message.reply_text(msg)

async def _analysis_clean_by_sports(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    sports_to_clear: set[str] | None,
    label: str,
):
    """
    tomorrow 시트에서 sport 컬럼 기준으로 특정 종목만 지우거나,
    sports_to_clear 가 None 이면 전체(헤더 제외) 삭제.
    """
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")

    if not (client_gs and spreadsheet_id):
        await update.message.reply_text(
            "구글시트 설정(GOOGLE_SERVICE_KEY 또는 SPREADSHEET_ID)이 없어 시트를 초기화할 수 없습니다."
        )
        return

    try:
        sh = client_gs.open_by_key(spreadsheet_id)
        ws = sh.worksheet(os.getenv("SHEET_TOMORROW_NAME", "tomorrow"))
    except Exception as e:
        await update.message.reply_text(f"tomorrow 시트를 열지 못했습니다: {e}")
        return

    try:
        rows = ws.get_all_values()
    except Exception as e:
        await update.message.reply_text(f"시트 읽기 오류: {e}")
        return

    # 데이터가 아예 없으면 헤더만 복구
    if not rows:
        header = ["sport", "id", "title", "summary"]
        try:
            ws.clear()
            ws.update(range_name="A1", values=[header])
        except Exception as e:
            await update.message.reply_text(f"시트 초기화 중 오류: {e}")
            return
        reload_analysis_from_sheet()
        await update.message.reply_text(f"tomorrow 시트를 초기화했습니다. ({label})")
        return

    header = rows[0]
    data_rows = rows[1:]

    # sport 컬럼 인덱스 찾기 (기본은 0)
    try:
        idx_sport = header.index("sport")
    except ValueError:
        idx_sport = 0

    kept_rows = [header]
    deleted_count = 0

    if sports_to_clear is None:
        # 전체 삭제 (헤더만 남김)
        deleted_count = len(data_rows)
    else:
        # 해당 종목만 제외하고 유지
        for row in data_rows:
            sport_val = row[idx_sport] if len(row) > idx_sport else ""
            if sport_val in sports_to_clear:
                deleted_count += 1
                continue
            kept_rows.append(row)

    try:
        ws.clear()
        ws.update(range_name="A1", values=kept_rows)
    except Exception as e:
        await update.message.reply_text(f"시트 쓰기 오류: {e}")
        return

    reload_analysis_from_sheet()

    if sports_to_clear is None:
        await update.message.reply_text(
            f"tomorrow 시트의 분석 데이터를 전체 초기화했습니다. (삭제된 행: {deleted_count}개)"
        )
    else:
        await update.message.reply_text(
            f"tomorrow 시트에서 {label} 분석 데이터만 초기화했습니다. (삭제된 행: {deleted_count}개)"
        )


async def _clean_tomorrow_sheet(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    sports_to_clear: set[str] | None,
    label: str,
):
    """기존 함수명 호환용 래퍼. 내부적으로 _analysis_clean_by_sports를 호출한다."""
    await _analysis_clean_by_sports(
        update,
        context,
        sports_to_clear=sports_to_clear,
        label=label,
    )

# ⚽ 축구 계열(해외축구 / K리그 / J리그)만 삭제
async def soccerclean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sports = {"해외축구", "K리그", "J리그"}
    await _analysis_clean_by_sports(
        update,
        context,
        sports_to_clear=sports,
        label="축구(해외축구/K리그/J리그)",
    )


# ⚾ 야구 계열(해외야구 / KBO / NPB)만 삭제
async def baseballclean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sports = {"해외야구", "KBO", "NPB"}
    await _analysis_clean_by_sports(
        update,
        context,
        sports_to_clear=sports,
        label="야구(해외야구/KBO/NPB)",
    )


# 🏀 농구만 삭제
async def basketclean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 농구 전체: 예전 '농구' + 새 라벨 'NBA', 'KBL'
    sports = {"농구", "NBA", "KBL"}
    await _clean_tomorrow_sheet(
        update,
        context,
        sports_to_clear=sports,
        label="농구(NBA/KBL)",
    )


# 🏐 배구만 삭제
async def volleyclean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sports = {"배구", "v리그"}
    await _analysis_clean_by_sports(
        update,
        context,
        sports_to_clear=sports,
        label="배구/v리그",
    )


# 기타 종목만 삭제 (기타 / 기타종 / 기타종목)
async def etcclean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sports = {"기타", "기타종", "기타종목"}
    await _analysis_clean_by_sports(
        update,
        context,
        sports_to_clear=sports,
        label="기타 종목",
    )


# tomorrow 시트 전체 분석 데이터 삭제 (헤더만 남김)
async def analysisclean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _analysis_clean_by_sports(
        update,
        context,
        sports_to_clear=None,
        label="전체 분석",
    )

# 🔹 4) /rollover – 내일 분석 → 오늘 분석으로 복사
async def rollover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    client = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")

    if client and spreadsheet_id:
        try:
            sh = client.open_by_key(spreadsheet_id)

            sheet_today_name = os.getenv("SHEET_TODAY_NAME", "today")
            sheet_tomorrow_name = os.getenv("SHEET_TOMORROW_NAME", "tomorrow")

            ws_today = sh.worksheet(sheet_today_name)
            ws_tomorrow = sh.worksheet(sheet_tomorrow_name)

            rows = ws_tomorrow.get_all_values()

            if rows:
                ws_today.clear()
                ws_today.update(range_name="A1", values=rows)

                header = rows[0]
                ws_tomorrow.clear()
                ws_tomorrow.update(range_name="A1", values=[header])
            else:
                print("[GSHEET] tomorrow 탭에 데이터가 없어 시트 롤오버는 생략합니다.")

        except Exception as e:
            print(f"[GSHEET] 롤오버 중 시트 복사 실패: {e}")

    else:
        print("[GSHEET] 클라이언트 또는 SPREADSHEET_ID 없음 → 시트 롤오버는 건너뜀.")

    reload_analysis_from_sheet()

    await update.message.reply_text(
        "✅ 롤오버 완료!\n"
        "구글시트 'tomorrow' 탭 내용을 'today' 탭으로 복사했고,\n"
        "'tomorrow' 탭은 헤더만 남기고 초기화했어.\n\n"
        "이제 오늘 경기 분석은 'today' 탭에서, 내일 경기는 'tomorrow' 탭에서 작성하면 돼."
    )


def simple_summarize(text: str, max_chars: int = 400) -> str:
    """
    아주 단순 요약: 문장 사이 공백 정리 후,
    max_chars 안쪽에서 '다.' 기준으로 잘라서 반환.
    (Gemini 오류 시 fallback 용)
    """
    if not text:
        return ""

    text = re.sub(r"\s+", " ", text).strip()

    if len(text) <= max_chars:
        return text

    cut = text.rfind("다.", 0, max_chars)
    if cut != -1:
        return text[: cut + 2]

    return text[:max_chars] + "..."

# 🔹 OpenAI 클라이언트 (요약용)
_openai_client = None

def get_openai_client():
    """
    OPENAI_API_KEY 환경변수 기반으로 OpenAI 클라이언트를 초기화해서 돌려준다.
    키가 없으면 None을 리턴하고, 에러 시 simple_summarize 폴백을 사용한다.
    """
    global _openai_client
    if _openai_client is not None:
        return _openai_client

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("[OPENAI] OPENAI_API_KEY 미설정 → simple_summarize 폴백 사용")
        return None

    try:
        _openai_client = OpenAI(api_key=api_key)
        print("[OPENAI] OpenAI 클라이언트 초기화 완료")
    except Exception as e:
        print(f"[OPENAI] 클라이언트 초기화 실패: {e}")
        _openai_client = None
    return _openai_client

# 🔹 mazgtv 홍보 문구/해시태그 공통 제거용 패턴
MAZ_REMOVE_PATTERNS = [
    # 기본 홍보 문구
    r"실시간\s*스포츠중계",
    r"스포츠\s*중계",
    r"스포츠\s*분석",
    r"스포츠\s*정보",
    r"라이브\s*스포츠중계",
    r"실시간\s*무료\s*중계",
    r"무료\s*중계",
    r"무료\s*스포츠중계",

    # 사이트/브랜드명
    r"마징가티비",
    r"마징가\s*티비",
    r"마징가TV",
    r"마징가\s*TV",
    r"마징가\s*티브이",
    r"마징가\s*티비\s*바로가기",

    # 배너/유도 문구
    r"배너\s*문의",
    r"배너",
    r"링크\s*클릭",
    r"바로가기",
    r"스포츠중계\s*바로가기",

    # 해시태그
    r"#\S+",

    # 날짜/제목 라인 (예: 11월 28일 프리뷰, 11월 28일 경기 분석)
    r"11월\s*\d{1,2}\s*[^\n]{0,30}",
    r"\d{1,2}월\s*\d{1,2}일\s*[^\n]{0,30}",

    # 제목 패턴 (중계 / 분석)
    r"[가-힣A-Za-z0-9 ]+ 중계",
    r"[가-힣A-Za-z0-9 ]+ 분석",
    r"[가-힣A-Za-z0-9 ]+ 프리뷰",

    # 섹션 제목들
    r"프리뷰",
    r"핵심\s*포인트",
    r"핵심\s*포인트\s*정리",
    r"승부\s*예측",
    r"베팅\s*강도",
    r"마무리\s*코멘트",
    r"마무리\s*정리",

    # 사이트로 유도하는 꼬리 문구
    r"에서\s*확인하세요[^\n]*",

    # 픽 라인 (승/무/패, 핸디, 언더오버)
    r"\[승/무/패\][^\n]+",
    r"\[핸디\][^\n]+",
    r"\[언더오버\][^\n]+",
    r"승패\s*추천[^\n]*",
    r"추천\s*픽[^\n]*",

    # 이모지/아이콘류
    r"✅",
    r"⭕",
    r"⚠️",
    r"⭐+",
    r"🔥",
    r"👉",
]

def clean_maz_text(text: str) -> str:
    """
    mazgtv 원문/요약에서 홍보 문구, 해시태그 등을 제거하고
    공백을 정리해서 돌려준다.
    """
    if not text:
        return ""
    for pattern in MAZ_REMOVE_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def extract_mmdd_from_kickoff(kickoff: str) -> tuple[int | None, int | None]:
    """
    '11-28 (금) 02:45' 같은 문자열에서 (month, day)를 뽑는다.
    다른 포맷(예: '11월 28일 02:45')도 대비해서 정규식 두 개를 시도.
    """
    if not kickoff:
        return (None, None)

    text = kickoff.strip()

    # 1) 11-28 (금) 02:45 형태
    m = re.search(r"(\d{1,2})\s*-\s*(\d{1,2})", text)
    if not m:
        # 2) 11월 28일 (금) 02:45 형태
        m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)

    if not m:
        return (None, None)

    try:
        month = int(m.group(1))
        day = int(m.group(2))
        return (month, day)
    except ValueError:
        return (None, None)

def ensure_team_line_breaks(body: str, home_team: str, away_team: str) -> str:
    """
    요약 본문에서 '홈팀: ... 원정팀:' 이 한 줄에 붙어 있을 때
    홈팀 블록 / 원정팀 블록 / 🎯 픽 사이에 빈 줄을 강제로 넣어 준다.
    """
    if not body:
        return body

    body = body.replace("\r\n", "\n")

    # 홈팀: ... 원정팀: 이 한 줄에 붙어 있으면 강제 분리
    if home_team and away_team:
        pattern = rf"({re.escape(home_team)}:[^\n]+)\s+({re.escape(away_team)}:)"
        body = re.sub(pattern, r"\1\n\n\2", body)

    # 원정팀: ... 🎯 픽 붙어 있으면 분리
    if away_team:
        pattern2 = rf"({re.escape(away_team)}:[^\n]+)\s+🎯\s*픽"
        body = re.sub(pattern2, r"\1\n\n🎯 픽", body)

    # 🎯 픽 라인을 항상 단독 줄로
    body = re.sub(r"\s*🎯\s*픽\s*", "\n\n🎯 픽\n", body)

    # 여러 공백 정리
    body = re.sub(r"[ \t]+", " ", body)
    return body.strip()


def _postprocess_analysis_body(body: str, home_label: str, away_label: str) -> str:
    """
    - 팀별 블록 사이 줄바꿈 강제
    - 🎯 픽 아래는 '➡' 로 시작하는 3줄만 남기기
    """
    body = ensure_team_line_breaks(body, home_label, away_label)

    if "🎯 픽" in body:
        head, tail = body.split("🎯 픽", 1)
        lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]

        # ➡ 로 시작하는 줄만 골라서 최대 3줄
        picks = [ln for ln in lines if ln.startswith("➡")]
        picks = picks[:3]

        if picks:
            tail_norm = "🎯 픽\n" + "\n".join(picks)
            body = head.rstrip() + "\n\n" + tail_norm
        else:
            # 픽이 이상하게 나오면 그냥 잘라버림
            body = head.rstrip()

    return body.strip()


def summarize_analysis_with_gemini(
    full_text: str,
    *,
    league: str = "해외축구",
    home_team: str = "",
    away_team: str = "",
    max_chars: int = 900,
) -> tuple[str, str]:
    """
    👉 이제는 OpenAI(gpt-4.1-mini)를 사용해서
       '제목 + 팀별 요약 + 🎯 픽' 형식으로 경기 분석을 생성한다.
    """
    client_oa = get_openai_client()

    # 기본 제목
    if home_team and away_team:
        base_title = f"[{league}] {home_team} vs {away_team} 경기 분석"
    else:
        base_title = f"[{league}] 해외축구 경기 분석"

    home_label = home_team or "홈팀"
    away_label = away_team or "원정팀"

    # 원문 정리
    full_text_clean = clean_maz_text(full_text or "").strip()
    if len(full_text_clean) > 7000:
        full_text_clean = full_text_clean[:7000]

    # OpenAI 키 없으면 간단 폴백
    if not client_oa:
        core = simple_summarize(full_text_clean, max_chars=max_chars)
        body = (
            f"{home_label}:\n{core}\n\n"
            "🎯 픽\n"
            "➡️ 경기 흐름 참고용 텍스트입니다.\n"
            "➡️ 실제 베팅 전 라인·부상 정보를 반드시 다시 확인해야 합니다.\n"
            "➡️ 세부 추천픽은 별도 분석이 필요합니다."
        )
        return (base_title or "[경기 분석]", body)

    # ── 프롬프트 ──
    prompt = f"""
다음은 해외축구 경기 분석 원문이다.
전체 내용을 이해한 뒤, 아래에 제시한 ‘엄격한 형식’ 그대로 작성하라.
원문 문장을 그대로 복사하지 말고 반드시 재작성하고, 형식에서 벗어나는 텍스트는 절대 출력하지 마라.

출력 형식은 아래를 정확히 지켜라:

제목: [리그] 홈팀 vs 원정팀 경기 분석
요약:
{home_label}:
- 문장1
- 문장2
(문장 수는 2~3개, 반드시 줄바꿈으로 구분)

{away_label}:
- 문장1
- 문장2
(문장 수는 2~3개)

🎯 픽
➡️ 홈팀/원정팀 승 관련 1줄
➡️ 핸디 관련 1줄
➡️ 오버/언더 관련 1줄

❗ 절대 금지:
- 픽 섹션에 설명문 추가 금지
- 픽을 3줄 초과하거나 3줄보다 적게 쓰는 것 금지
- {home_label}/{away_label} 블록 사이 줄바꿈 누락 금지
- 팀 이름 없이 분석 시작 금지
- 🎯 픽 위에 불필요한 텍스트 출력 금지
- 형식과 다른 여분 문장 출력 금지

아래는 리그/팀 정보다.
리그: {league}
홈팀: {home_label}
원정팀: {away_label}

===== 경기 분석 원문 =====
{full_text_clean}
""".strip()

    try:
        resp = client_oa.chat.completions.create(
            model=os.getenv("OPENAI_MODEL_ANALYSIS", "gpt-4.1-mini"),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "너는 축구 경기 분석을 요약해서 정리하는 한국어 전문가다. "
                        "문장은 간결하고 직설적으로 쓰고, 형식을 반드시 지킨다."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_completion_tokens=700,
        )
        text_out = (resp.choices[0].message.content or "").strip()
        if not text_out:
            raise ValueError("empty response from OpenAI (analysis)")

        # 제목 / 요약 분리
        m_title = re.search(r"제목\s*[:：]\s*(.+)", text_out)
        m_body = re.search(r"요약\s*[:：]\s*(.+)", text_out, flags=re.S)

        new_title = (m_title.group(1).strip() if m_title else "").strip()
        body = (m_body.group(1).strip() if m_body else text_out).strip()

        if not new_title:
            new_title = base_title or "[경기 분석]"

        # 제목이 본문에 또 반복되면 잘라내기
        body = remove_title_prefix(new_title, body)

        # 형식 강제 후처리 (팀별 줄바꿈 + 픽 3줄)
        body = _postprocess_analysis_body(body, home_label, away_label)

        if len(body) > max_chars + 200:
            body = body[: max_chars + 200]

        return (new_title, body)

    except Exception as e:
        print(f"[OPENAI][ANALYSIS] 실패 → simple_summarize 폴백: {e}")
        core = simple_summarize(full_text_clean, max_chars=max_chars)
        body = (
            f"{home_label}:\n{core}\n\n"
            "🎯 픽\n"
            "➡️ 경기 흐름 참고용 텍스트입니다.\n"
            "➡️ 실제 베팅 전 라인·부상 정보를 반드시 다시 확인해야 합니다.\n"
            "➡️ 세부 추천픽은 별도 분석이 필요합니다."
        )
        return (base_title or "[경기 분석]", body)
        
def rewrite_for_site_openai(
    full_text: str,
    *,
    league: str = "",
    home_team: str = "",
    away_team: str = "",
    max_chars: int = 3500,
) -> tuple[str, str]:
    """
    사이트 게시용: '원문 기반 재작성' 전용.

    ✅ 목표
    - 원문을 그대로 복붙하지 않고, 구조화된 서술형(팀 분석/경기 흐름/핵심 포인트/최종 픽)으로 재작성
    - 원문 사실(부상/전술/기록/선수 등)에서 벗어나는 임의 생성 금지
    - 매치업 표기에서 'vs/VS/대' 같은 구분자 사용 금지(팀명 공백 연결)

    ⚠️ 금지
    - '고트티비', 'GOATTV', 'goat-tv' 등 특정 사이트/브랜드명 언급 금지
    """
    client_oa = get_openai_client()

    full_text_clean = clean_maz_text(full_text or "").strip()
    if not full_text_clean:
        return ("[분석글 없음]", "")

    # 너무 길면 잘라서 토큰 폭주 방지
    if len(full_text_clean) > 9000:
        full_text_clean = full_text_clean[:9000]

    # 제목 기본값(표시용: 구분자 없이 팀명 공백 연결)
    _league = (league or "").strip() or "경기"
    if home_team and away_team:
        base_title = f"[{_league}] {home_team} {away_team} 스포츠분석"
    else:
        base_title = f"[{_league}] 스포츠분석"

    # OpenAI 키 없으면 최소 폴백(=원문 기반 요약)만 반환
    if not client_oa:
        body_fb = simple_summarize(full_text_clean, max_chars=max_chars)
        # 안전장치: 브랜드/구분자 제거
        body_fb = _postprocess_site_body_text(body_fb)
        return (base_title, body_fb)

    home_label = (home_team or "").strip() or "홈팀"
    away_label = (away_team or "").strip() or "원정팀"

    # (선택) 하단 고정 문구: 필요하면 Render/GitHub 환경변수로 교체 가능
    # 예: SITE_FOOTER_LINE="이번 경기는 스포츠분석 커뮤니티 오분을 통해 보다 정확한 정보를 참고하면 도움이 될 것이다."
    footer_line_env = (os.getenv("SITE_FOOTER_LINE") or "").strip()
    if footer_line_env.lower() in ("0", "off", "false", "none", "no"):
        footer_line = ""
    elif footer_line_env:
        footer_line = footer_line_env
    else:
        footer_line = _pick_site_footer_line(seed=f"{_league}|{home_label}|{away_label}|{full_text_clean[:200]}")

    prompt = f"""아래는 스포츠 경기 분석 원문이다.
원문 내용을 바탕으로 **사이트 게시용 서술형 분석글**로 재작성하라.

필수 요구사항:
- 원문 문장을 그대로 복사하지 말고, 반드시 재작성
- 원문 내용과 어긋나는 '사실'을 만들지 말 것 (선수/전술/부상/기록 등 임의 생성 금지)
- 문장 흐름은 자연스럽게, 단락을 명확히 분리
- 아래 섹션 구성은 유지하되, 원문에 없는 섹션 정보는 과장하지 말 것
- '스포츠분석' 키워드를 본문에 **1~2회** 자연스럽게 포함 (과도한 반복 금지)
- '고트티비/GOATTV/goat-tv' 등 특정 사이트/브랜드명은 **절대** 언급하지 말 것
- 매치업 표기에서 'vs', 'VS', '대' 같은 구분자를 사용하지 말 것.
  → 예: '{home_label} {away_label}' 처럼 **팀명을 공백으로만 연결**해 표기할 것
- 섹션 제목에 '팀1/팀2'라는 표현을 절대 쓰지 말고, **반드시 팀명**을 넣어라
- 결과는 **오직 본문만** 출력 (추가 안내/주석 금지)

출력 구조(섹션 제목은 그대로 사용):
[{home_label} 분석]
(3~6문장)

───────────────

[{away_label} 분석]
(3~6문장)

───────────────

[경기 흐름 전망]
(4~8문장)

───────────────

[핵심 포인트 요약]
- 항목 1
- 항목 2
- 항목 3

───────────────

[최종 픽]
- 승패: (원문에 방향성이 있으면 그 방향 유지, 없으면 '보류')
- 핸디: (원문에 방향성이 있으면 그 방향 유지, 없으면 '보류')
- 언오버: (원문에 방향성이 있으면 그 방향 유지, 없으면 '보류')

경기 정보:
- 리그: {_league}
- 홈팀: {home_label}
- 원정팀: {away_label}

===== 원문 =====
{full_text_clean}""".strip()

    # footer를 쓰는 경우에만, "마지막 줄"에 고정 문구를 넣도록 강제
    if footer_line:
        prompt += "\n\n마지막 줄에 다음 문장을 그대로 1회만 추가하라:\n" + footer_line.strip()

    try:
        resp = client_oa.chat.completions.create(
            model=os.getenv("OPENAI_MODEL_SITE", os.getenv("OPENAI_MODEL_ANALYSIS", "gpt-4.1-mini")),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "너는 스포츠 분석 글을 사이트 게시용으로 재작성하는 한국어 लेखक이다. "
                        "원문 사실에서 벗어나지 않고, 문장을 간결하고 직설적으로 쓴다."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_completion_tokens=1200,
        )

        body = (resp.choices[0].message.content or "").strip()
        if not body:
            raise ValueError("empty response from OpenAI (site)")

        # 팀명 헤더 강제 치환(모델이 [팀1 분석]/[팀2 분석]로 출력하는 경우 대비)
        body = re.sub(r"\[\s*팀1\s*분석\s*\]", f"[{home_label} 분석]", body)
        body = re.sub(r"\[\s*팀2\s*분석\s*\]", f"[{away_label} 분석]", body)
        # 혹시 모델이 제목을 섞어 출력하면 제거
        body = re.sub(r"^제목\s*[:：].*\n+", "", body).strip()

        # ✅ 마지막 안전 처리: 브랜드/구분자 제거 + footer 위치 정리
        body = _postprocess_site_body_text(body, footer_line=footer_line)

        # 너무 길면 자르기
        if len(body) > max_chars:
            body = body[:max_chars].rstrip()

        return (base_title, body)

    except Exception as e:
        print(f"[OPENAI][SITE] 재작성 실패 → simple_summarize 폴백: {e}")
        body_fb = simple_summarize(full_text_clean, max_chars=max_chars)
        body_fb = _postprocess_site_body_text(body_fb, footer_line=footer_line)
        return (base_title, body_fb)



def summarize_with_gemini(full_text: str, orig_title: str = "", max_chars: int = 400) -> tuple[str, str]:
    """
    뉴스 기사용 요약 함수.
    이제 OpenAI(gpt-4.1-mini)를 사용해서
    '제목: ... / 요약: ...' 형식으로 리라이팅한다.
    """
    client_oa = get_openai_client()
    trimmed = (full_text or "").strip()
    if len(trimmed) > 6000:
        trimmed = trimmed[:6000]

    # 키 없으면 폴백
    if not client_oa:
        print("[OPENAI][NEWS] 클라이언트 없음 → simple_summarize 사용")
        fb_summary = simple_summarize(trimmed, max_chars=max_chars)
        fb_summary = clean_maz_text(fb_summary)
        return (orig_title or "[제목 없음]", fb_summary)

    prompt = (
        "다음은 스포츠 뉴스 기사 원문과 기존 제목이다.\n"
        "전체 내용을 이해한 뒤, 새로운 한국어 뉴스 헤드라인 1개와 2~3문장짜리 요약을 작성해줘.\n"
        "기사 앞부분을 그대로 복사하지 말 것.\n"
        f"요약 길이는 공백 포함 {max_chars}자 내외.\n"
        "\n"
        "반드시 아래 형식으로만 출력해:\n"
        "제목: (여기에 새 제목)\n"
        "요약: (여기에 요약문)\n"
        "그 외의 문장은 출력하지 마.\n"
        "\n"
        "===== 기존 제목 =====\n"
        f"{orig_title}\n"
        "\n"
        "===== 기사 원문 =====\n"
        f"{trimmed}\n"
    )

    try:
        resp = client_oa.chat.completions.create(
            model=os.getenv("OPENAI_MODEL_NEWS", "gpt-4.1-mini"),
            messages=[
                {
                    "role": "system",
                    "content": "너는 스포츠 뉴스를 간결하게 요약하는 한국어 기자다. "
                               "형식을 정확히 지키고, 중복 표현은 줄인다.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
            max_completion_tokens=450,
        )
        text_out = (resp.choices[0].message.content or "").strip()
        if not text_out:
            raise ValueError("empty response from OpenAI (news)")

        new_title = ""
        summary = ""
        for line in text_out.splitlines():
            line = line.strip()
            if line.startswith("제목:"):
                new_title = line[len("제목:"):].strip(" ：:")
            elif line.startswith("요약:"):
                summary = line[len("요약:"):].strip(" ：:")

        if not summary:
            summary = text_out

        if len(summary) > max_chars + 100:
            summary = summary[: max_chars + 100]

        if not new_title:
            new_title = orig_title or "[제목 없음]"

        summary = clean_maz_text(summary)
        return (new_title, summary)

    except Exception as e:
        print(f"[OPENAI][NEWS] 요약 실패 → simple_summarize로 폴백: {e}")
        fb_summary = simple_summarize(trimmed, max_chars=max_chars)
        fb_summary = clean_maz_text(fb_summary)
        return (orig_title or "[제목 없음]", fb_summary)

def extract_main_text_from_html(soup: BeautifulSoup) -> str:
    """
    mazgtv 분석 상세 페이지에서 본문 텍스트를 최대한 잘 뽑아서 리턴.
    HTML 구조를 정확히 모를 때를 대비해서 여러 후보 셀렉터를 시도하고,
    그래도 없으면 body 전체 텍스트를 사용.
    """
    # 광고/스크립트 제거
    for bad in soup.select("script, style, noscript"):
        try:
            bad.decompose()
        except Exception:
            pass

    candidates = [
        "div.ql-editor",      # 에디터 본문일 때 자주 쓰는 클래스
        "div.v-card__text",   # vuetify 카드 본문
        "div.article-body",
        "div.view-cont",
        "div#content",
        "article",
        "main",
    ]

    for sel in candidates:
        el = soup.select_one(sel)
        if not el:
            continue
        text = el.get_text("\n", strip=True)
        if len(text) >= 200:   # 너무 짧으면 본문이 아닐 가능성
            return re.sub(r"\s+", " ", text).strip()

    # 후보들에서 못 찾으면 bodyFallback
    body = soup.body or soup
    text = body.get_text("\n", strip=True)
    return re.sub(r"\s+", " ", text).strip()

# ───────────────── Daum harmony API 공통 함수 ─────────────────

async def fetch_daum_news_json(client: httpx.AsyncClient, category_id: str, size: int = 20) -> list[dict]:
    """
    다음 스포츠 harmony API에서 특정 카테고리 ID의 뉴스 JSON 리스트를 가져온다.
    (해외축구, KBO, 해외야구, 농구, 배구 공통)
    """
    if not category_id:
        return []

    base_url = "https://sports.daum.net/media-api/harmony/contents.json"

    today_kst = get_kst_now().date()
    ymd = today_kst.strftime("%Y%m%d")
    create_dt = f"{ymd}000000~{ymd}235959"

    discovery_tag_value = json.dumps({
        "group": "media",
        "key": "defaultCategoryId3",
        "value": str(category_id),
    }, ensure_ascii=False)

    params = {
        "page": 0,
        "consumerType": "HARMONY",
        "status": "SERVICE",
        "createDt": create_dt,
        "size": size,
        "discoveryTag[0]": discovery_tag_value,
    }

    r = await client.get(base_url, params=params, timeout=10.0)
    r.raise_for_status()
    data = r.json()

    contents = None
    if isinstance(data, dict):
        contents = data.get("contents")
        if contents is None:
            inner = data.get("data") or data.get("result") or data.get("body")
            if isinstance(inner, dict):
                contents = inner.get("contents") or inner.get("list") or inner.get("items")
    elif isinstance(data, list):
        contents = data

    if not contents:
        print("[CRAWL][DAUM] JSON 구조를 파악하지 못했습니다. 최상위 키:",
              list(data.keys()) if isinstance(data, dict) else type(data))
        return []

    return contents


async def fetch_article_body(client: httpx.AsyncClient, url: str) -> str:
    """
    (예전 네이버용) 뉴스 상세 페이지에서 본문 텍스트만 추출.
    현재는 사용하지 않지만 남겨둠.
    """
    try:
        r = await client.get(url, timeout=10.0, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except Exception as e:
        print(f"[CRAWL][ARTICLE] 요청 실패: {url} / {e}")
        return ""

    soup = BeautifulSoup(r.text, "html.parser")

    body = soup.select_one("#newsEndContents")
    if body:
        return body.get_text("\n", strip=True)

    body = soup.select_one("#newsEndBody")
    if body:
        return body.get_text("\n", strip=True)

    body = soup.select_one("#dic_area")
    if body:
        return body.get_text("\n", strip=True)

    print(f"[CRAWL][ARTICLE] 본문 셀렉터 매치 실패: {url}")
    return ""


async def crawl_daum_news_common(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    category_id: str,
    sport_label: str,
    max_articles: int = 10,
):
    """
    Daum harmony API + HTML 본문을 이용해 뉴스 크롤링 후
    구글시트 news 탭에 저장하는 공통 함수.
    """
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    if not category_id:
        await update.message.reply_text(
            f"{sport_label} 카테고리 ID가 설정되어 있지 않습니다.\n"
            "코드 상단 DAUM_CATEGORY_IDS 또는 환경변수를 확인해 주세요."
        )
        return

    await update.message.reply_text(
        f"다음스포츠 {sport_label} 뉴스를 크롤링합니다. 잠시만 기다려 주세요..."
    )

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
        ) as client:
            await _maz_warmup(client)
            contents = await fetch_daum_news_json(client, category_id, size=max_articles)

            if not contents:
                await update.message.reply_text(f"{sport_label} JSON 데이터에서 기사를 찾지 못했습니다.")
                return

            articles: list[dict] = []

            # 1) JSON에서 제목 + 기사 URL 추출
            for item in contents:
                if not isinstance(item, dict):
                    continue

                title = (
                    item.get("title")
                    or item.get("contentTitle")
                    or item.get("headline")
                    or item.get("name")
                )

                url = (
                    item.get("contentUrl")
                    or item.get("permalink")
                    or item.get("url")
                    or item.get("link")
                )

                if not title or not url:
                    continue

                title = str(title).strip()
                url = str(url).strip()

                if url.startswith("/"):

                    url = urljoin("https://sports.daum.net", url)

                articles.append({"title": title, "link": url})

                if len(articles) >= max_articles:
                    break

            if not articles:
                await update.message.reply_text(
                    f"JSON은 받았지만, {sport_label} 제목/URL 정보를 찾지 못했습니다."
                )
                return

            # (추가) news_cafe_queue 동시 적재 (원문 제목/URL 저장) - 기존 news 탭 흐름은 그대로 유지
            try:
                enq_cnt = enqueue_news_to_cafe_queue(sport_label=sport_label, articles=articles)
                if enq_cnt:
                    print(f"[NEWS_QUEUE] {sport_label} {enq_cnt}건 적재")
            except Exception as _e:
                print(f"[NEWS_QUEUE] enqueue 실패: {_e}")

            # 2) 각 기사 페이지 들어가서 본문 크롤링 + 요약
            for art in articles:
                try:
                    r2 = await client.get(art["link"], timeout=10.0)
                    r2.raise_for_status()
                    s2 = BeautifulSoup(r2.text, "html.parser")

                    body_el = (
                        s2.select_one("div#harmonyContainer")
                        or s2.select_one("section#article-view-content-div")
                        or s2.select_one("div.article_view")
                        or s2.select_one("div#mArticle")
                        or s2.find("article")
                        or s2.body
                    )

                    raw_body = ""
                    if body_el:
                        # 이미지 설명 캡션 제거
                        try:
                            for cap in body_el.select(
                                "figcaption, .txt_caption, .photo_desc, .caption, "
                                "em.photo_desc, span.caption, p.caption"
                            ):
                                try:
                                    cap.extract()
                                except Exception:
                                    pass
                        except Exception:
                            # select가 안 되는 경우는 그냥 무시
                            pass

                        raw_body = body_el.get_text("\n", strip=True)
                    
                    clean_text = clean_daum_body_text(raw_body)
                    clean_text = remove_title_prefix(art["title"], clean_text)
                    
                    # ✅ Gemini로 "새 제목 + 요약" 생성 (400자 내외)
                    new_title, new_summary = summarize_with_gemini(
                        clean_text,
                        orig_title=art["title"],
                        max_chars=400,
                    )

                    art["title"] = new_title
                    art["summary"] = new_summary

                except Exception as e:
                    print(f"[CRAWL][DAUM] 기사 파싱 실패 ({art['link']}): {e}")
                    # 크롤링 실패 시에도 최소한 뭔가 넣어두기
                    art["summary"] = "(본문 크롤링 실패)"

    except Exception as e:
        _log_httpx_exception("[MAZ][Exception]", e)
        await update.message.reply_text(f"요청 오류가 발생했습니다: {e}")
        return

    # 3) 구글 시트 저장
    client_gs = get_gs_client()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")

    if not (client_gs and spreadsheet_id):
        await update.message.reply_text(
            "구글시트 설정(GOOGLE_SERVICE_KEY 또는 SPREADSHEET_ID)이 없어 시트에 저장하지 못했습니다."
        )
        return

    try:
        sh = client_gs.open_by_key(spreadsheet_id)
        ws = sh.worksheet(os.getenv("SHEET_NEWS_NAME", "news"))
    except Exception as e:
        await update.message.reply_text(f"뉴스 시트를 열지 못했습니다: {e}")
        return

    rows_to_append = []
    for art in articles:
        rows_to_append.append([
            sport_label,      # sport
            "",               # id
            art["title"],     # title
            art["summary"],   # summary
        ])

    try:
        ws.append_rows(rows_to_append, value_input_option="RAW", table_range="A1")
    except Exception as e:
        await update.message.reply_text(f"시트 쓰기 오류: {e}")
        return

    await update.message.reply_text(
        f"다음스포츠 {sport_label} 뉴스 {len(rows_to_append)}건을 저장했습니다.\n"
        "/syncsheet 로 텔레그램 메뉴를 갱신할 수 있습니다."
    )

# ───────────────── mazgtv 분석 공통 (내일 경기 → today/tomorrow 시트, JSON/API 버전) ─────────────────

# 상세 API 실제 경로에 맞게 여기만 수정하면 됨
MAZ_DETAIL_API_TEMPLATE = f"{MAZ_BASE_URL}/api/board/{{board_id}}"


def _parse_game_start_date(game_start_at: str) -> date | None:
    """
    '2025-11-28T05:00:00' 같은 문자열에서 날짜(date)만 뽑는다.
    """
    if not game_start_at:
        return None
    try:
        # 뒤에 타임존이 붙어 있어도 앞 19자리까지만 잘라서 파싱
        s = game_start_at[:19]
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        return dt.date()
    except Exception:
        return None

from datetime import date  # 파일 위쪽에 이미 있을 수도 있음

def detect_game_date_from_item(item: dict, target_date: date) -> date | None:
    """
    mazgtv 리스트 JSON 한 건(item) 전체를 훑으면서
    target_date 와 '같은 날짜'가 들어있는지 찾는다.

    아래 패턴들 중 하나라도 target_date 와 같으면 target_date 를 리턴, 
    하나도 없으면 None:
    - YYYY-MM-DD
    - MM-DD
    - M월 D일 / MM월 DD일
    """

    def _iter_values(x):
        if isinstance(x, dict):
            for v in x.values():
                yield from _iter_values(v)
        elif isinstance(x, list):
            for v in x:
                yield from _iter_values(v)
        else:
            yield x

    texts = [v for v in _iter_values(item) if isinstance(v, str)]

    ty = target_date.year

    # 1) YYYY-MM-DD 패턴들 중에서 target_date 와 같은 날짜가 있는지
    for text in texts:
        for yy, mm, dd in re.findall(r"(\d{4})-(\d{2})-(\d{2})", text):
            try:
                dt = date(int(yy), int(mm), int(dd))
            except ValueError:
                continue
            if dt == target_date:
                return dt

    # 2) MM-DD (예: 12-03)
    for text in texts:
        for mm, dd in re.findall(r"(\d{1,2})-(\d{1,2})", text):
            try:
                dt = date(ty, int(mm), int(dd))
            except ValueError:
                continue
            if dt == target_date:
                return dt

    # 3) '12월 3일' / '12 월 03 일' 패턴
    for text in texts:
        for mm, dd in re.findall(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", text):
            try:
                dt = date(ty, int(mm), int(dd))
            except ValueError:
                continue
            if dt == target_date:
                return dt

    return None

def classify_basketball_volleyball_sport(league: str) -> str:
    """
    mazgtv leagueName 기준으로 ANALYSIS 시트 sport 값을 결정한다.
    - NBA      → "NBA"
    - KBL      → "KBL"
    - WKBL     → "WKBL"
    - V-리그   → "V리그"
    - 그 외 배구 관련 → "배구"
    - 그 외 농구 관련 → "농구"
    """
    if not league:
        return "농구"

    upper = league.upper()

    # NBA
    if "NBA" in upper:
        return "NBA"

    # 국내 농구
    if "KBL" in upper:
        return "KBL"
    if "WKBL" in upper:
        return "WKBL"

    # 배구 (V리그/해외배구 포함)
    if any(x in upper for x in ["V-리그", "V리그", "V-LEAGUE", "VOLLEY", "배구"]):
        # 국내 V리그 표시를 조금 더 명확히 하고 싶으면 여기 분리
        if "V" in upper or "V-LEAGUE" in upper:
            return "V리그"
        return "배구"

    # 나머지는 대충 농구로 묶기
    if any(x in upper for x in ["BASKET", "농구"]):
        return "농구"

    # 정말 정보가 없으면 농구로
    return "농구"

async def crawl_maz_analysis_common(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    base_url: str,
    sport_label: str,
    league_default: str,
    day_key: str = "tomorrow",
    max_pages: int = 5,
    board_type: int = 2,
    category: int = 1,
    target_ymd: str | None = None,
    export_site: bool = False,
):
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    # ✅ 날짜 기준 설정 (today/tomorrow)
    if target_ymd is None:
        base_date = get_kst_now().date()
        if day_key == "tomorrow":
            base_date += timedelta(days=1)
        target_ymd = base_date.strftime("%Y-%m-%d")

    target_date = datetime.strptime(target_ymd, "%Y-%m-%d").date()

    await update.message.reply_text(
        f"mazgtv {sport_label} 분석 페이지에서 {target_ymd} 경기 분석글을 가져옵니다. 잠시만 기다려 주세요..."
    )

    rows_to_append: list[list[str]] = []

    # ✅ 중복 방지: 이미 today/tomorrow 시트에 있는 src_id 모으기
    existing_ids = get_existing_analysis_ids(day_key)

    # ✅ site_export 시트 중복 방지용
    export_sheet_name = EXPORT_TODAY_SHEET_NAME if day_key == "today" else EXPORT_TOMORROW_SHEET_NAME
    existing_export_src_ids = get_existing_export_src_ids(export_sheet_name) if export_site else set()
    site_rows_to_append: list[list[str]] = []

    try:
        async with httpx.AsyncClient(
            headers=BROWSER_HEADERS,
            follow_redirects=True,
        ) as client:
            await _maz_warmup(client)

            for page in range(1, max_pages + 1):
                list_url = (
                    f"{MAZ_LIST_API}"
                    f"?page={page}&perpage=20"
                    f"&boardType={board_type}&category={category}"
                    f"&sort=b.game_start_at+DESC,+b.created_at+DESC"
                )

                r = await client.get(list_url, timeout=10.0)
                r.raise_for_status()

                try:
                    data = r.json()
                except Exception as e:
                    print(f"[MAZ][LIST] JSON 파싱 실패(page={page}): {e}")
                    print("  응답 일부:", r.text[:200])

                    # 응답이 HTML이면(<!doctype ...>) API 경로/권한/헤더 변화 가능성이 큽니다.
                    head = (r.text or "").lstrip()[:200].lower()
                    if head.startswith("<!doctype") or head.startswith("<html"):
                        try:
                            await update.message.reply_text(
                                "⚠️ mazgtv 목록 API가 JSON이 아니라 HTML을 반환합니다.\n"
                                "도메인 변경(mazgtv3) 이후 API 경로/요청 방식이 바뀌었을 가능성이 큽니다.\n\n"
                                f"- 현재 MAZ_LIST_API: {MAZ_LIST_API}\n"
                                "브라우저 개발자도구(Network → Fetch/XHR)에서 '목록'을 불러오는 JSON 요청 URL을 확인해서\n"
                                "그 URL을 MAZ_LIST_API 환경변수로 지정한 뒤 다시 시도해 주세요."
                            )
                        except Exception:
                            pass
                        break

                    # HTML이 아니면 일시적 오류일 수 있으니 다음 페이지로 진행
                    continue

                if isinstance(data, dict):
                    items = (
                        data.get("rows")
                        or (data.get("data") or {}).get("rows")
                        or data.get("list")
                        or data.get("items")
                    )
                else:
                    items = data

                if not isinstance(items, list) or not items:
                    print(f"[MAZ][LIST] page={page} 항목 없음 → 반복 종료")
                    break

                for item in items:
                    if not isinstance(item, dict):
                        continue

                    board_id = item.get("id")
                    if not board_id:
                        continue

                    row_id = f"maz_{board_id}"

                    # ✅ 중복 처리
                    needs_analysis = row_id not in existing_ids
                    needs_export = bool(export_site) and (row_id not in existing_export_src_ids)
                    if (not needs_analysis) and (not needs_export):
                        print(f"[MAZ][SKIP_DUP] already exists (analysis+export): {row_id}")
                        continue
                    if (not needs_analysis) and needs_export:
                        print(f"[MAZ][BACKFILL] analysis exists but export missing: {row_id}")

                    game_start_at = (
                        item.get("gameStartAt")
                        or item.get("game_start_at")
                        or ""
                    )
                    game_start_at = str(game_start_at).strip()

                    game_start_at_text = str(item.get("gameStartAtText") or "").strip()
                    print(
                        f"[MAZ][DEBUG] page={page} id={board_id} "
                        f"gameStartAt='{game_start_at}' gameStartAtText='{game_start_at_text}'"
                    )

                    # 1) gameStartAt로 날짜 파싱
                    item_date = _parse_game_start_date(game_start_at)

                    # 2) 실패하면 item 전체에서 날짜 패턴 탐색 (연도 보정용)
                    if not item_date:
                        item_date = detect_game_date_from_item(item, target_date)

                    print(f"[MAZ][DEBUG_DATE] page={page} id={board_id} item_date={item_date}")

                    if not item_date:
                        continue

                    # ✅ 날짜 필터링
                    # - 축구/농구/배구: target_date와 정확히 일치만
                    # - 야구: (혹시 주간 카드로 들어오는 경우) 일치가 아니면 같은 주(0~6일)까지 허용
                    if sport_label == "야구":
                        if item_date != target_date:
                            delta_days = (target_date - item_date).days
                            if delta_days < 0 or delta_days >= 7:
                                continue
                    else:
                        if item_date != target_date:
                            continue

                    league = item.get("leagueName") or league_default
                    home = item.get("homeTeamName") or ""
                    away = item.get("awayTeamName") or ""

                    detail_url = MAZ_DETAIL_API_TEMPLATE.format(board_id=board_id)
                    try:
                        r2 = await client.get(detail_url, timeout=10.0)
                        r2.raise_for_status()
                        detail = r2.json()
                    except Exception as e:
                        print(f"[MAZ][DETAIL] id={board_id} 요청 실패: {e}")
                        continue

                    content_html = detail.get("content") or ""
                    if not str(content_html).strip():
                        print(f"[MAZ][DETAIL] id={board_id} content 없음")
                        continue

                    soup = BeautifulSoup(content_html, "html.parser")
                    try:
                        for bad in soup.select("script, style, .ad, .banner"):
                            bad.decompose()
                    except Exception:
                        pass

                    full_text = soup.get_text("\n", strip=True)
                    full_text = clean_maz_text(full_text)
                    if not full_text:
                        print(f"[MAZ][DETAIL] id={board_id} 본문 텍스트 없음")
                        continue

                    if needs_analysis:
                        new_title, new_body = summarize_analysis_with_gemini(
                            full_text,
                            league=league,
                            home_team=home,
                            away_team=away,
                            max_chars=900,
                        )
                    else:
                        new_title, new_body = "", ""

                    # ✅ today/tomorrow 크롤링 제목 앞에 날짜 프리픽스 추가 (중복 방지)
                    if new_title and day_key in ("today", "tomorrow"):
                        _dp = f"{target_date.month}월 {target_date.day}일 "
                        if not str(new_title).startswith(_dp):
                            new_title = _dp + str(new_title).strip()

                    # ✅ sport 세부 분류
                    row_sport = sport_label

                    if sport_label == "축구":
                        if "K리그" in league:
                            row_sport = "K리그"
                        elif "J리그" in league:
                            row_sport = "J리그"
                        else:
                            row_sport = "해외축구"

                    elif sport_label == "야구":
                        upper_league = (league or "").upper()
                        if "KBO" in upper_league:
                            row_sport = "KBO"
                        elif "NPB" in upper_league:
                            row_sport = "NPB"
                        elif "MLB" in upper_league:
                            row_sport = "해외야구"
                        else:
                            row_sport = "해외야구"

                    elif sport_label in ("농구", "농구/배구"):
                        row_sport = classify_basketball_volleyball_sport(league or "")

                    if needs_analysis:
                        rows_to_append.append([row_sport, row_id, new_title, new_body])

                    # ✅ 사이트 업로드용(site_export)도 같이 저장
                    if export_site and needs_export:
                        # export 시트에만 백필/저장
                        try:
                            _tmp_title, site_body = rewrite_for_site_openai(
                                full_text,
                                league=league,
                                home_team=home,
                                away_team=away,
                            )
                        except Exception as e:
                            print(f"[SITE_EXPORT][ERR] id={board_id}: {e}")
                        else:
                            # ✅ 팀명/구분자 정규화 (표시용 키워드: '팀1 팀2')
                            _norm_key = infer_norm_sport_key(sport_label, row_sport, league or "")
                            _league_for_title = (league or league_default or "").strip()
                            site_title = build_export_title(target_date, _league_for_title, home, away, _norm_key)
                            # body(E열)에도 팀명/구분자 표기를 정리(FC/CF/워리어스 등 제거 + vs/대 제거)
                            site_body = normalize_text_teamnames(site_body, sport_key=_norm_key, home_raw=home, away_raw=away)
                            site_body = _postprocess_site_body_text(site_body)
                    
                            # ✅ export 시트 G열(simple) 생성: 팀 태그/해시태그 유지
                            try:
                                _hd, _ad, _ = build_matchup_display(home, away, _norm_key)
                                site_simple = build_dynamic_cafe_simple(
                                    site_title,
                                    site_body,
                                    sport=row_sport,
                                    seed=str(row_id),
                                    home_team=_hd,
                                    away_team=_ad,
                                )
                            except Exception:
                                site_simple = ""

                            # ✅ E열(body) 하단에 해시태그를 같이 붙이기(원하는 형식)
                            #   - G열(simple) 마지막 줄은 해시태그 라인으로 생성됨
                            try:
                                _last_line = (site_simple or "").strip().splitlines()[-1].strip()
                                if _last_line.startswith("#") and _last_line not in (site_body or ""):
                                    site_body = (site_body or "").rstrip() + "\n\n" + _last_line
                            except Exception:
                                pass
                    
                            site_rows_to_append.append([
                                day_key,
                                row_sport,
                                row_id,
                                site_title,
                                site_body,
                                get_kst_now().strftime("%Y-%m-%d %H:%M:%S"),
                                site_simple,
                            ])
                            existing_export_src_ids.add(row_id)

    except Exception as e:
        # ✅ 여기 except는 try와 같은 들여쓰기 레벨이어야 함
        await update.message.reply_text(f"요청 오류가 발생했습니다: {e}")
        return

    if (not rows_to_append) and (not site_rows_to_append):
        await update.message.reply_text(
            f"mazgtv {sport_label} 분석에서 {target_ymd} 경기 분석글을 찾지 못했습니다."
        )
        return

    if rows_to_append:
        ok = append_analysis_rows(day_key, rows_to_append)
        if not ok:
            await update.message.reply_text("구글시트에 분석 데이터를 저장하지 못했습니다.")
            return
    else:
        ok = True

    # ✅ site_export 시트 저장
    if export_site and site_rows_to_append:
        ok2 = append_export_rows(export_sheet_name, site_rows_to_append)
        if not ok2:
            await update.message.reply_text("site_export 시트 저장 중 오류가 발생했습니다.")
            return

    reload_analysis_from_sheet()

    extra = ""
    if export_site:
        extra = f"\\nexport 시트에도 {len(site_rows_to_append)}건을 저장했습니다."

    saved_analysis_cnt = len(rows_to_append)
    saved_export_cnt = len(site_rows_to_append) if export_site else 0
    await update.message.reply_text(
        f"mazgtv {sport_label} 분석에서 {target_ymd} 저장 완료: "
        f"분석시트 {saved_analysis_cnt}건, export {saved_export_cnt}건." + extra + "\n"
        "텔레그램에서 경기 분석픽 메뉴를 열어 확인해보세요."
    )

# ───────────────── 종목별 (Daum 뉴스) 크롤링 명령어 ─────────────────

# 해외축구
async def crawlsoccer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat_id = DAUM_CATEGORY_IDS.get("world_soccer")
    await crawl_daum_news_common(
        update,
        context,
        category_id=cat_id,
        sport_label="축구",
        max_articles=5,
    )


# 국내축구 (K리그 등, 5개)
async def crawlsoccerkr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat_id = DAUM_CATEGORY_IDS.get("soccer_kleague")
    await crawl_daum_news_common(
        update,
        context,
        category_id=cat_id,
        sport_label="축구",   # 해외/국내를 한 카테고리에 묶어서 보여주기
        max_articles=5,
    )


# KBO 야구
async def crawlbaseball(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat_id = DAUM_CATEGORY_IDS.get("baseball_kbo")
    await crawl_daum_news_common(
        update,
        context,
        category_id=cat_id,
        sport_label="야구",
        max_articles=5,
    )


# 해외야구 (MLB 등)
async def crawloverbaseball(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat_id = DAUM_CATEGORY_IDS.get("baseball_world")
    await crawl_daum_news_common(
        update,
        context,
        category_id=cat_id,
        sport_label="야구",  # 필요하면 '해외야구'로 분리해서도 가능
        max_articles=5,
    )


# 농구
async def crawlbasketball(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat_id = DAUM_CATEGORY_IDS.get("basketball")
    await crawl_daum_news_common(
        update,
        context,
        category_id=cat_id,
        sport_label="농구",
        max_articles=10,
    )


# 배구
async def crawlvolleyball(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat_id = DAUM_CATEGORY_IDS.get("volleyball")
    await crawl_daum_news_common(
        update,
        context,
        category_id=cat_id,
        sport_label="배구",
        max_articles=10,
    )


# ───────────────── news_cafe_queue → 네이버 카페 업로드 (/cafe_news_upload) ─────────────────

def _safe_truncate(s: str, n: int = 300) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def _news_is_rate_limited(err: str) -> bool:
    s = (err or "").lower()
    # 네이버 OpenAPI 쪽은 429 / rate / limit / too many 등으로 오는 경우가 많아 보수적으로 체크
    return ("429" in s) or ("rate" in s) or ("limit" in s) or ("too many" in s)


def fetch_daum_article_text_and_image(url: str, orig_title: str = "") -> tuple[str, str]:
    """다음/다음스포츠/다음뉴스(v.daum.net 포함) 기사 URL에서
    - 본문 텍스트
    - 대표 이미지 URL(가능하면)
    를 추출한다.

    ⚠️ 주의:
    - v.daum.net(다음뉴스) 페이지는 div#harmonyContainer가 없을 수 있어,
      본문 컨테이너(body_el) 기준으로 첫 img를 추가로 탐색한다.
    """
    if not url:
        return "", ""

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        # 일부 CDN이 referer 없는 호출을 막는 케이스가 있어 안전장치로 넣음
        "Referer": url,
    }

    r = requests.get(url, headers=headers, timeout=25)
    r.raise_for_status()

    # requests가 간혹 encoding을 못 잡는 케이스가 있어 UTF-8로 폴백
    try:
        if not r.encoding:
            r.encoding = "utf-8"
    except Exception:
        pass

    soup = BeautifulSoup(r.text, "html.parser")

    def _pick_img_attr(tag) -> str:
        if not tag:
            return ""
        return (
            (tag.get("src") or "")
            or (tag.get("data-src") or "")
            or (tag.get("data-original") or "")
            or (tag.get("data-lazy-src") or "")
            or (tag.get("data-original-src") or "")
        ).strip()

    def _norm_img(u: str) -> str:
        from urllib.parse import urljoin
        u = (u or "").strip()
        u = html.unescape(u)
        u = u.strip(" \"'")
        if not u:
            return ""
        if u.startswith("data:"):
            return ""
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("/"):
            u = urljoin(url, u)
        return u

    # 0) 본문 컨테이너 후보(텍스트/이미지 공용)
    body_el = (
        soup.select_one("div#harmonyContainer")
        or soup.select_one("section#article-view-content-div")
        or soup.select_one("div.article_view")
        or soup.select_one("div#mArticle")
        or soup.find("article")
        or soup.body
    )

    # 1) 대표 이미지 URL 추출 우선순위
    img_url = ""

    # 1-a) og:image(가장 안정적)
    for prop in ("og:image", "og:image:secure_url"):
        try:
            meta = soup.find("meta", attrs={"property": prop})
            if meta and meta.get("content"):
                img_url = _norm_img(str(meta.get("content")).strip())
                if img_url:
                    break
        except Exception:
            pass

    # 1-b) twitter:image 폴백
    if not img_url:
        try:
            meta = soup.find("meta", attrs={"name": "twitter:image"})
            if meta and meta.get("content"):
                img_url = _norm_img(str(meta.get("content")).strip())
        except Exception:
            pass

    # 1-c) 본문 컨테이너 내 첫 img 폴백(특히 v.daum.net 대응)
    if not img_url:
        try:
            if body_el:
                img = body_el.find("img")
                if img:
                    img_url = _norm_img(_pick_img_attr(img))
        except Exception:
            pass

    # 1-d) 최후 폴백: 페이지 전체에서 첫 img
    if not img_url:
        try:
            img = soup.find("img")
            if img:
                img_url = _norm_img(_pick_img_attr(img))
        except Exception:
            pass

    # 2) 본문 텍스트 추출(크롤링 로직 재사용)
    raw_body = ""
    if body_el:
        # 이미지 설명 캡션 제거
        try:
            for cap in body_el.select(
                "figcaption, .txt_caption, .photo_desc, .caption, "
                "em.photo_desc, span.caption, p.caption"
            ):
                try:
                    cap.extract()
                except Exception:
                    pass
        except Exception:
            pass

        raw_body = body_el.get_text("\n", strip=True)

    clean_text = clean_daum_body_text(raw_body)
    if orig_title:
        clean_text = remove_title_prefix(orig_title, clean_text)

    return clean_text, img_url

def _download_image_bytes(img_url: str, *, referer: str = "") -> tuple[bytes, str, str]:
    """이미지 URL을 다운로드해서 (bytes, filename, mime_type) 반환. 실패하면 (b"", "", "").

    ✅ 다운로드 안정성
    - User-Agent/Referer 포함
    - daumcdn thumb URL(?fname=...)이면 원본 URL을 우선 시도하고, 실패 시 thumb로 폴백
    - Content-Type이 image/*가 아니면 '파일 시그니처'로 2차 판정(HTML 다운로드 방지)

    ✅ 카페 API 업로드 호환성
    - 일부 CDN은 Accept 헤더에 webp/avif가 포함되면 webp로 내려주는 경우가 있습니다.
      네이버 카페 Open API multipart 이미지 첨부는 JPEG/PNG 계열이 가장 안정적이라,
      기본 Accept는 webp/avif를 광고하지 않도록 설정합니다.
    - 혹시 webp로 받아진 경우에는 (가능하면) JPEG로 변환해서 반환합니다.
    """
    if not img_url:
        return b"", "", ""

    # --- URL 후보 만들기(원본 우선) ---
    from urllib.parse import urlparse, parse_qs, unquote, urljoin

    raw = html.unescape((img_url or "").strip()).strip(" \"'")
    if not raw or raw.startswith("data:"):
        return b"", "", ""
    if raw.startswith("//"):
        raw = "https:" + raw
    if raw.startswith("/") and referer:
        raw = urljoin(referer, raw)

    candidates: list[str] = []
    # thumb URL이면 fname 원본 먼저
    try:
        pr = urlparse(raw)
        qs = parse_qs(pr.query or "")
        fname = (qs.get("fname", [""]) or [""])[0]
        if fname:
            orig = unquote(fname)
            # fname가 2중 인코딩인 케이스가 있어 1~2회 추가 디코딩
            for _ in range(2):
                if "%2F" in orig or "%3A" in orig or "%3a" in orig:
                    orig = unquote(orig)
            if orig and orig.startswith("//"):
                orig = "https:" + orig
            if orig and orig.startswith("http"):
                candidates.append(orig)
    except Exception:
        pass

    candidates.append(raw)

    # 중복 제거(순서 유지)
    seen = set()
    cand2 = []
    for u in candidates:
        u2 = (u or "").strip()
        if not u2 or u2 in seen:
            continue
        seen.add(u2)
        cand2.append(u2)

    headers = {
        "User-Agent": "Mozilla/5.0",
        # ✅ webp/avif를 광고하지 않음(= JPG/PNG로 받게 유도)
        "Accept": "image/jpeg,image/png,image/gif,image/*;q=0.8,*/*;q=0.5",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer

    def _sniff_mime(data: bytes) -> str:
        if not data:
            return ""
        if data[:3] == b"\xFF\xD8\xFF":
            return "image/jpeg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        # AVIF/HEIC 간단 시그니처(ftyp) 탐지
        if len(data) >= 16 and data[4:8] == b"ftyp":
            brand = data[8:12]
            if brand in (b"avif", b"avis"):
                return "image/avif"
            if brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"):
                return "image/heic"
        return ""

    def _convert_webp_to_jpeg(data: bytes) -> bytes:
        """webp 등을 JPEG로 변환. Pillow가 없으면 b'' 반환."""
        try:
            from PIL import Image  # type: ignore
            from io import BytesIO
            im = Image.open(BytesIO(data))
            # 투명도 처리
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                im = im.convert("RGBA")
                bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg.convert("RGB")
            else:
                im = im.convert("RGB")
            out = BytesIO()
            im.save(out, format="JPEG", quality=92, optimize=True)
            return out.getvalue()
        except Exception:
            return b""

    last_err = ""
    for u in cand2:
        try:
            r = requests.get(u, headers=headers, timeout=25, stream=False)
            r.raise_for_status()

            data = r.content or b""
            if not data:
                last_err = "EMPTY_IMAGE"
                continue

            content_type = (r.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()

            # Content-Type이 image가 아니면 시그니처로 판정(HTML 다운로드 방지)
            if not content_type.startswith("image/"):
                sniff = _sniff_mime(data)
                if not sniff:
                    last_err = f"NOT_IMAGE:{content_type or 'unknown'}"
                    continue
                content_type = sniff

            # ✅ webp/avif 등은 네이버 카페 업로드가 실패할 수 있어 가능하면 jpeg로 변환
            if content_type in ("image/webp", "image/avif", "image/heic"):
                conv = _convert_webp_to_jpeg(data)
                if conv:
                    data = conv
                    content_type = "image/jpeg"

            # 확장자 결정
            ext = ".jpg"
            if "png" in content_type:
                ext = ".png"
            elif "gif" in content_type:
                ext = ".gif"
            else:
                ext = ".jpg"

            filename = f"news_image{ext}"
            return data, filename, content_type

        except Exception as e:
            last_err = str(e)

    print(f"[NEWS_IMAGE] download 실패: {img_url} / last_err={last_err}")
    return b"", "", ""


def _needs_url_decode(s: str) -> bool:
    s = s or ""
    # %HH 형태가 있으면 URL 인코딩 문자열일 가능성이 높다.
    return bool(re.search(r"%[0-9A-Fa-f]{2}", s))


def _safe_url_decode(s: str) -> str:
    """퍼센트 인코딩된 문자열(예: %ED%92%80%EB%9F%BC...)을 사람이 읽을 수 있게 복원한다.
    - 일반 문자열은 그대로 반환한다.
    """
    t = (s or "").strip()
    if not t:
        return ""
    if _needs_url_decode(t):
        try:
            return (unquote_plus(t) or t).strip()
        except Exception:
            return t
    return t


def _seo_phrase_for_sport(sport_label: str) -> str:
    """종목 문자열을 바탕으로 본문에 자연스럽게 넣을 '종목 키워드(SEO)' 문구를 만든다."""
    s = (sport_label or "").strip()
    if not s:
        return "스포츠뉴스"

    sl = s.lower()
    # 축구
    if ("축구" in s) or ("soccer" in sl):
        if ("해외" in s) or ("epl" in sl) or ("laliga" in sl) or ("분데스" in s) or ("챔피언스" in s):
            return "해외축구 뉴스"
        if ("k리그" in sl) or ("k리그" in s) or ("k-league" in sl) or ("국내" in s):
            return "국내축구 소식"
        return "축구 뉴스"

    # 야구
    if ("야구" in s) or ("baseball" in sl):
        if ("kbo" in sl) or ("프로" in s) or ("국내" in s):
            return "프로야구 소식"
        if ("mlb" in sl) or ("해외" in s):
            return "해외야구 소식"
        return "야구 소식"

    # 농구
    if ("농구" in s) or ("basket" in sl):
        if ("nba" in sl):
            return "NBA 소식"
        if ("kbl" in sl) or ("프로" in s) or ("국내" in s):
            return "프로농구 소식"
        return "농구 뉴스"

    # 배구
    if ("배구" in s) or ("volley" in sl):
        if ("v리그" in sl) or ("v리그" in s) or ("프로" in s) or ("국내" in s):
            return "프로배구 소식"
        return "배구 뉴스"

    return "스포츠뉴스"


def _clean_news_rewrite_text_keep_newlines(text: str) -> str:
    """뉴스 재작성 본문용 클리너.
    - 줄바꿈/문단 구조는 유지
    - 과도한 공백만 정리
    - 해시태그(#...)는 삭제하지 않는다
    """
    if not text:
        return ""
    t = str(text)
    t = t.replace("\r\n", "\n").replace("\r", "\n")

    # 제어문자 제거
    t = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", t)

    # 라인별 앞뒤 공백 정리 + 탭/연속 공백 축소
    lines = []
    for ln in t.split("\n"):
        ln2 = re.sub(r"[ \t]+", " ", ln).strip()
        lines.append(ln2)

    t = "\n".join(lines)

    # 너무 많은 연속 줄바꿈은 2개로 축소
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t


def _extract_hashtags_fallback(body: str, sport_label: str, max_tags: int = 10) -> list[str]:
    """OpenAI 출력에 해시태그가 없을 때의 폴백 생성.
    - 본문에서 자주 등장하는 고유명사/키워드를 단순 추출
    - 너무 일반적인 단어는 제외
    """
    if max_tags < 6:
        max_tags = 6
    base = []

    # 종목 기본 태그
    base.append("스포츠뉴스")
    phrase = _seo_phrase_for_sport(sport_label)
    if phrase:
        base.append(phrase.replace(" ", ""))

    # 토큰 후보: 한글/영문/숫자 2~20자
    tokens = re.findall(r"[가-힣A-Za-z0-9]{2,20}", body or "")
    stop = {
        "그리고","하지만","그러나","또한","이번","지난","오늘","내일","현재","이날","이후","관련","소식","뉴스","기사",
        "경기","시즌","리그","구단","선수","감독","팀","상대","이적","전망","분석","스포츠","스포츠뉴스","해외축구","프로야구",
        "등","것","수","때","더","중","대한","대한민국","한국","대한축구협회","프로야구소식","해외축구뉴스",
    }

    from collections import Counter
    cnt = Counter()
    for tok in tokens:
        t = tok.strip()
        if not t:
            continue
        if t in stop:
            continue
        if re.fullmatch(r"\d+", t):
            continue
        # 너무 짧은 영문 약어(예: 'vs') 제거
        if t.lower() in {"vs","v","tv","go","or","an","as","to","in","on","at","of","is"}:
            continue
        cnt[t] += 1

    # 빈도 상위 + 길이가 적당한 것 우선
    extras = []
    for w, _n in cnt.most_common(30):
        # 너무 긴 토큰은 제외
        if len(w) > 16:
            continue
        extras.append(w)
        if len(extras) >= (max_tags - len(base)):
            break

    tags_raw = base + extras
    # 중복 제거(순서 유지)
    seen = set()
    tags = []
    for t in tags_raw:
        k = t.strip()
        if not k:
            continue
        if k in seen:
            continue
        seen.add(k)
        tags.append(k)
        if len(tags) >= max_tags:
            break

    # 최소 6개 보장
    if len(tags) < 6:
        for add in ["이적소식", "경기결과", "리그소식", "팀소식", "선수소식", "스포츠분석"]:
            if add not in seen:
                tags.append(add)
                seen.add(add)
            if len(tags) >= 6:
                break

    return tags[:max_tags]


def _format_hashtags(tags: list[str], per_line: int = 4) -> str:
    tags = [t for t in (tags or []) if (t or "").strip()]
    if not tags:
        return ""
    per_line = max(3, int(per_line or 4))
    lines = []
    for i in range(0, len(tags), per_line):
        chunk = tags[i:i+per_line]
        lines.append(" ".join([f"#{t.replace(' ', '')}" for t in chunk]))
    return "\n".join(lines).strip()


def _has_enough_hashtags(body: str) -> bool:
    # '#단어'가 6개 이상이면 OK로 본다.
    tags = re.findall(r"#[^\s#]{2,}", body or "")
    return len(tags) >= 6

def _looks_too_similar_to_source(rewritten: str, source: str) -> bool:
    """재작성 결과가 원문과 지나치게 유사한지(복붙 위험) 매우 단순하게 검사한다.
    - 완전한 표절 판정은 아니며, '긴 구간이 그대로 남은' 케이스를 2차 방어하기 위한 휴리스틱.
    """
    try:
        out = re.sub(r"\s+", " ", (rewritten or "")).strip()
        src = re.sub(r"\s+", " ", (source or "")).strip()
        if len(out) < 600 or len(src) < 600:
            return False

        # 해시태그 섹션은 비교에서 제외
        if "[해시태그]" in out:
            out = out.split("[해시태그]", 1)[0].strip()

        win_len = 45
        if len(out) <= win_len:
            return False

        step = max(40, len(out) // 8)
        hits = 0
        for pos in range(0, len(out) - win_len, step):
            w = out[pos:pos + win_len]
            if w and (w in src):
                hits += 1
                if hits >= 2:
                    return True
        return False
    except Exception:
        return False


def rewrite_news_full_with_openai(
    full_text: str,
    *,
    orig_title: str,
    sport_label: str,
    has_image: bool = False,
) -> tuple[str, str]:
    """원문 텍스트를 기반으로 '완전 재작성' 본문을 생성한다. (title, body_text)

    목표(이미지 유무와 관계없이 공통):
    - 문장/구조/흐름을 새로 쓰는 완전 재작성
    - 최소 1,200자~2,500자 분량(이미지 없는 글은 더 충분히)
    - 섹션 구조 + 키워드 자연 삽입 + 하단 해시태그 6~10개
    """
    client_oa = get_openai_client()
    trimmed = (full_text or "").strip()
    if len(trimmed) > 9000:
        trimmed = trimmed[:9000]

    # 길이 가이드(이미지 없는 글은 더 길게 유도)
    min_chars = 1200 if has_image else 1500
    max_chars = 2500

    # 키 없으면(극히 예외) 최소 폴백: 구조만이라도 잡되, 표절 위험이 있어 운영상 OpenAI 키 설정을 권장
    if not client_oa:
        core = simple_summarize(trimmed, max_chars=700)
        sport_phrase = _seo_phrase_for_sport(sport_label)
        body_fb = (
            "[기사 요약]\n"
            f"{core}\n\n"
            "[핵심 포인트]\n"
            "- 핵심 이슈가 부각됐다\n"
            "- 관련 팀/선수의 선택이 변수로 떠올랐다\n"
            "- 향후 일정과 성적에 영향이 예상된다\n\n"
            "[상세 내용 및 배경]\n"
            "원문에서 언급된 배경을 토대로, 현재 상황이 어떤 맥락에서 등장했는지 정리했다.\n\n"
            "[현재 상황 분석]\n"
            f"이번 {sport_phrase} 이슈는 팬 반응과 현장 평가가 엇갈릴 수 있다. 스포츠뉴스 흐름에서 중요한 변수들을 점검할 필요가 있다.\n\n"
            "[전망 및 의미]\n"
            "단기적으로는 경기 운영과 로테이션에, 중장기적으로는 스쿼드 구성과 전략에 영향을 줄 수 있다.\n\n"
            "[해시태그]\n"
            + _format_hashtags(_extract_hashtags_fallback(core, sport_label, max_tags=8))
        )
        title_fb = orig_title or "스포츠 뉴스"
        return (_safe_url_decode(title_fb), _clean_news_rewrite_text_keep_newlines(body_fb))

    sport_phrase = _seo_phrase_for_sport(sport_label)

    def _make_prompt(strict: bool = False) -> str:
        strict_line = (
            "- 특히 원문 문장을 10어절 이상 연속으로 그대로 쓰면 안 된다(표절 위험).\n"
            if strict else
            "- 원문 문장을 길게 그대로 복사하지 말 것(문단 단위 복사 금지).\n"
        )
        return (
            "아래는 스포츠 뉴스 기사 원문이다. 원문을 그대로 베끼지 말고, 의미만 참고해서 문장/표현/구성/흐름을 "
            "전부 새로 만들어 '완전 재작성' 기사로 써줘.\n\n"
            "필수 요구사항:\n"
            "- 한국어로 작성\n"
            f"- 길이: 공백 포함 약 {min_chars}~{max_chars}자(너무 짧게 끝내지 말 것)\n"
            "- 아래 섹션 제목을 **그대로 사용**하고, 각 섹션은 충분한 분량으로 작성\n"
            "- 키워드는 본문 문맥 속에서 자연스럽게 1~2회씩 포함: '스포츠뉴스', '" + sport_phrase + "', '스포츠분석'\n"
            "- 원문에 나온 선수/팀/리그/감독 등 고유명사를 적절히 활용(단, 사실을 새로 만들지 말 것)\n"
            "- 과장/추측 최소화(원문에 근거해 서술)\n"
            + strict_line +
            "\n"
            "권장 구조(형식 가이드):\n"
            "[기사 요약]\n"
            "- 핵심을 2~3문단으로 자연스럽게 풀어 설명\n\n"
            "[핵심 포인트]\n"
            "- 3~5개 불릿(각 1문장)\n\n"
            "[상세 내용 및 배경]\n"
            "- 배경/맥락/과거 흐름/리그·팀 상황 등을 충분히\n\n"
            "[현재 상황 분석]\n"
            "- 현재 시점 의미, 변수, 반응 등을 분석적으로\n\n"
            "[전망 및 의미]\n"
            "- 향후 전개 가능성, 팀/리그에 미칠 영향\n\n"
            "[해시태그]\n"
            "- 본문 기반 핵심 키워드 6~10개를 해시태그(#)로만 출력\n\n"
            "반드시 아래 형식으로만 출력:\n"
            "제목: (새 제목 1개)\n"
            "본문:\n"
            "(여기에 본문)\n\n"
            f"===== 종목 =====\n{sport_label}\n\n"
            f"===== 기존 제목 =====\n{orig_title}\n\n"
            f"===== 기사 원문 =====\n{trimmed}\n"
        )

    last_exc = None
    for attempt in range(2):
        prompt = _make_prompt(strict=(attempt == 1))
        try:
            resp = client_oa.chat.completions.create(
                model=os.getenv("OPENAI_MODEL_NEWS_LONG", os.getenv("OPENAI_MODEL_NEWS", "gpt-4.1-mini")),
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "너는 스포츠 전문 기자이자 에디터다. "
                            "표절 위험이 없도록 완전히 새로운 문장으로 재작성하며, 문단/소제목/불릿/해시태그 구조를 지킨다."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.65,
                max_completion_tokens=2100,
            )
            out = (resp.choices[0].message.content or "").strip()
            if not out:
                raise ValueError("empty response from OpenAI (news_long)")

            new_title = ""
            body_lines: list[str] = []
            lines = out.splitlines()

            body_started = False
            for line in lines:
                t = line.strip()
                if t.startswith("제목:") and not new_title:
                    new_title = t[len("제목:"):].strip(" ：:")
                    continue
                if t.startswith("본문:") and not body_started:
                    body_started = True
                    rest = t[len("본문:"):].lstrip()
                    if rest:
                        body_lines.append(rest)
                    continue
                if body_started:
                    body_lines.append(line)

            body = "\n".join(body_lines).strip() if body_lines else out

            # 제목/본문 후처리(줄바꿈 유지 + URL 인코딩 문자열 방지)
            new_title = _safe_url_decode(new_title or orig_title or "스포츠 뉴스")
            body = _clean_news_rewrite_text_keep_newlines(body)

            # 해시태그 보강(없으면 폴백 생성해서 하단에 추가)
            if not _has_enough_hashtags(body):
                tags = _extract_hashtags_fallback(body, sport_label, max_tags=9)
                # 기존 [해시태그] 섹션이 이미 있으면 제거 후 재삽입(중복 방지)
                body_no_tags = body
                if "[해시태그]" in body_no_tags:
                    body_no_tags = body_no_tags.split("[해시태그]", 1)[0].rstrip()
                body = body_no_tags.rstrip() + "\n\n[해시태그]\n" + _format_hashtags(tags, per_line=4)

            # 품질 체크: 길이 / 섹션 / 불릿
            need_sections = all(sec in body for sec in ["[기사 요약]", "[핵심 포인트]", "[상세 내용 및 배경]", "[현재 상황 분석]", "[전망 및 의미]"])
            bullet_cnt = len([ln for ln in body.splitlines() if ln.strip().startswith("-")])
            if _looks_too_similar_to_source(body, trimmed):
                continue

            if (len(body) >= min_chars) and need_sections and (bullet_cnt >= 3):
                return new_title, body

        except Exception as e:
            last_exc = e
            continue

    # 최종 폴백(여기까지 오면 OpenAI가 계속 실패한 케이스)
    print(f"[OPENAI][NEWS_LONG] 재작성 실패(2회) → 폴백: {last_exc}")
    core = simple_summarize(trimmed, max_chars=900)
    body_fb = (
        "[기사 요약]\n"
        f"{core}\n\n"
        "[핵심 포인트]\n"
        "- 주요 이슈가 확인됐다\n"
        "- 핵심 인물/팀의 선택이 관전 포인트다\n"
        "- 일정/전력 변수에 따라 흐름이 달라질 수 있다\n\n"
        "[상세 내용 및 배경]\n"
        "원문에서 언급된 배경과 맥락을 바탕으로 사건의 흐름을 재구성했다.\n\n"
        "[현재 상황 분석]\n"
        f"이번 이슈는 {sport_phrase} 관점에서 해석 포인트가 있다. 스포츠뉴스 흐름 속에서 변수와 반응을 함께 봐야 한다.\n\n"
        "[전망 및 의미]\n"
        "향후 결과는 성적, 전력 구성, 여론에 영향을 줄 수 있다.\n\n"
        "[해시태그]\n"
        + _format_hashtags(_extract_hashtags_fallback(core, sport_label, max_tags=8))
    )
    return (_safe_url_decode(orig_title or "스포츠 뉴스"), _clean_news_rewrite_text_keep_newlines(body_fb))


def _make_cafe_center_html(text_body: str, raw_prefix_html: str = "") -> tuple[str, str]:
    """카페 업로드용 HTML 생성.

    - 기존 방식(줄바꿈 유지 + 깨짐 방지)을 그대로 사용하되,
    - raw_prefix_html(예: 이미지 태그 블록)을 <center> 내부 최상단에 "그대로" 삽입할 수 있게 확장.
      (text_body는 안전하게 escape 처리)
    """
    content_norm = (text_body or "").strip()

    # normalize newlines + strip simple html if any
    content_norm = content_norm.replace("\r\n", "\n").replace("\r", "\n")
    content_norm = re.sub(r"<br\s*/?>", "\n", content_norm, flags=re.I)
    content_norm = re.sub(r"</(p|div|li)>", "\n", content_norm, flags=re.I)
    content_norm = re.sub(r"<[^>]+>", "", content_norm)
    content_norm = content_norm.replace("&nbsp;", " ").strip()

    safe = html.escape(content_norm)
    lines = safe.split("\n") if safe else [""]
    html_lines = [(ln if ln.strip() else "&nbsp;") for ln in lines]

    prefix = (raw_prefix_html or "").strip()
    if prefix:
        # prefix가 이미 <br>로 끝나지 않으면 한 줄 띄우기
        if not re.search(r"<br\s*/?>\s*$", prefix, flags=re.I):
            prefix += "<br>"

    content_html = "<center>" + prefix + "<br>".join(html_lines) + "</center>"
    return content_html, content_norm


def _queue_update_status(ws_q, row_num: int, status: str, posted_at: str = "", error: str = "") -> None:
    """news_cafe_queue의 해당 행 상태 업데이트."""
    try:
        ws_q.update(range_name=f"E{row_num}:G{row_num}", values=[[status, posted_at, error]], value_input_option="RAW")
        return
    except Exception:
        pass

    # 헤더가 변경된 케이스 대비(느리지만 안전)
    try:
        header = ws_q.row_values(1)
        def _idx(name: str, fallback: int) -> int:
            try:
                return header.index(name) + 1  # 1-based
            except ValueError:
                return fallback
        c_status = _idx("status", 5)
        c_posted = _idx("postedAt", 6)
        c_error = _idx("error", 7)
        ws_q.update_cell(row_num, c_status, status)
        ws_q.update_cell(row_num, c_posted, posted_at)
        ws_q.update_cell(row_num, c_error, error)
    except Exception as e:
        print(f"[GSHEET] queue status update error row={row_num}: {e}")


async def cafe_news_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cafe_news_upload [N|latest|all] : news_cafe_queue의 NEW 항목을 뉴스 전용 계정으로 menuId=31에 업로드.

    추가 기능:
    - 주제 중복 필터(제목 유사도 → 본문 첫부분 해시)
    - 대표 이미지가 있을 경우 본문 최상단에 1장 삽입 + 가운데 정렬(#0 플레이스홀더)
    """
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    if not _naver_news_have_config():
        await update.message.reply_text(
            "뉴스용 네이버 토큰 설정이 없습니다.\n"
            "환경변수: NAVER_NEWS_CLIENT_ID / NAVER_NEWS_CLIENT_SECRET / NAVER_NEWS_REFRESH_TOKEN / NAVER_CAFE_CLUBID 를 확인해주세요."
        )
        return

    ws_q = get_news_cafe_queue_ws()
    if not ws_q:
        await update.message.reply_text("news_cafe_queue 시트를 열지 못했습니다.")
        return

    try:
        vals = ws_q.get_all_values()
    except Exception as e:
        await update.message.reply_text(f"news_cafe_queue 읽기 오류: {e}")
        return

    if not vals or len(vals) <= 1:
        await update.message.reply_text("news_cafe_queue에 업로드할 데이터가 없습니다.")
        return

    header = vals[0]

    def _hidx(name: str, fallback: int) -> int:
        try:
            return header.index(name)
        except ValueError:
            return fallback

    idx_created = _hidx("createdAt", 0)
    idx_sport = _hidx("sport", 1)
    idx_title = _hidx("title", 2)
    idx_url = _hidx("url", 3)
    idx_status = _hidx("status", 4)
    idx_error = _hidx("error", 6)

    # ── args 파싱
    n = 5
    mode_all = False
    if context.args:
        arg = (context.args[0] or "").strip().lower()
        if arg == "latest":
            n = 1
        elif arg == "all":
            mode_all = True
        else:
            try:
                n = int(arg)
                if n <= 0:
                    n = 5
            except Exception:
                n = 5

    # ── 유틸: queue error만 갱신(상태는 변경하지 않음)
    def _queue_append_error_only(row_num: int, reason: str, current_error: str = "") -> None:
        try:
            old = (current_error or "").strip()
            if reason and (reason in old):
                return
            new_err = reason if not old else (old + " | " + reason)
            ws_q.update_cell(row_num, idx_error + 1, new_err)
        except Exception as e:
            print(f"[GSHEET] queue error update fail row={row_num}: {e}")

    # ── 1차: 제목 유사도 기반 주제 중복 필터
    STOPWORDS = {
        "단독", "속보", "공식", "입장", "전망", "인터뷰", "전했다", "밝혔다", "밝혔다고", "말했다", "말한",
        "알렸다", "발표", "확정", "오피셜", "논란", "충격", "반전", "단신", "기자",
        # 자주 붙는 군더더기
        "오늘", "어제", "내일", "이번", "최근", "최신", "현지", "보도", "소식", "이슈",
    }

    def _norm_title_for_dedup(t: str) -> str:
        s = _safe_url_decode(t or "")
        s = s.strip()
        if not s:
            return ""
        # 괄호/대괄호/따옴표 내용 포함 통째로 제거(잡음 제거)
        s = re.sub(r"\([^)]*\)", " ", s)
        s = re.sub(r"\[[^\]]*\]", " ", s)
        s = re.sub(r"[\"'“”‘’]", " ", s)

        # 날짜/숫자 제거
        s = re.sub(r"\d{1,4}[./-]\d{1,2}[./-]\d{1,2}", " ", s)  # 2026.01.28 등
        s = re.sub(r"\d+", " ", s)

        # 특수문자 제거(한글/영문/공백만 남김)
        s = re.sub(r"[^0-9A-Za-z가-힣\s]", " ", s)

        # 다중 공백 정리
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _title_tokens(t: str) -> list[str]:
        s = _norm_title_for_dedup(t)
        if not s:
            return []
        toks = []
        for w in s.split():
            if w in STOPWORDS:
                continue
            # 너무 짧은 토큰은 잡음으로 처리
            if len(w) <= 1:
                continue
            toks.append(w)
        return toks

    def _title_similarity(a: dict, b: dict) -> float:
        """difflib + Jaccard 중 큰 값을 사용."""
        ta = a.get("_toks") or []
        tb = b.get("_toks") or []
        sa = " ".join(ta)
        sb = " ".join(tb)
        if not sa or not sb:
            return 0.0

        try:
            import difflib
            seq = difflib.SequenceMatcher(None, sa, sb).ratio()
        except Exception:
            seq = 0.0

        set_a = set(ta)
        set_b = set(tb)
        inter = len(set_a & set_b)
        uni = len(set_a | set_b)
        jac = (inter / uni) if uni else 0.0

        return max(seq, jac)

    title_thr = float(os.getenv("NEWS_DUP_TITLE_SIM_THRESHOLD", "0.8"))

    # ── NEW 로드(이미 DUP 표시된 건은 아예 처리 대상에서 제외)
    items = []
    total_new = 0
    for i, row in enumerate(vals[1:], start=2):  # row number in sheet
        st = (row[idx_status] if len(row) > idx_status else "").strip().upper()
        if st != "NEW":
            continue
        total_new += 1

        url = _normalize_news_url(row[idx_url] if len(row) > idx_url else "")
        if not url:
            continue

        created_at = (row[idx_created] if len(row) > idx_created else "").strip()
        sport = (row[idx_sport] if len(row) > idx_sport else "").strip()
        title_raw = (row[idx_title] if len(row) > idx_title else "").strip()
        title = _safe_url_decode(title_raw)
        err = (row[idx_error] if len(row) > idx_error else "").strip()

        # 이미 중복(SKIP)로 표시한 항목은 재처리하지 않음(상태는 NEW 유지)
        if err.startswith("DUP_TOPIC_TITLE") or err.startswith("DUP_TOPIC_BODY"):
            continue

        items.append(
            {
                "row": i,
                "createdAt": created_at,
                "sport": sport,
                "title": title,
                "url": url,
                "error": err,
            }
        )

    if not items:
        await update.message.reply_text("news_cafe_queue에 처리 가능한 status=NEW 항목이 없습니다.")
        return

    def _parse_iso(s: str) -> datetime:
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return datetime(1970, 1, 1, tzinfo=KST)

    # 최신 우선
    items.sort(key=lambda x: (_parse_iso(x["createdAt"]), x["row"]), reverse=True)

    # 제목 토큰 준비 + 제목 기반 중복 제거
    kept = []
    dup_by_title = []
    for it in items:
        it["_toks"] = _title_tokens(it["title"])
        is_dup = False
        for k in kept:
            # 종목이 다르면 비교하지 않음(오탐 방지)
            if (k.get("sport") or "").strip() != (it.get("sport") or "").strip():
                continue
            if _title_similarity(it, k) >= title_thr:
                is_dup = True
                dup_by_title.append(it)
                break
        if not is_dup:
            kept.append(it)

    # ── 로그 워크시트(선택)
    ws_log = get_news_cafe_log_ws()
    posted_urls = _load_news_cafe_posted_urls(ws_log) if ws_log else set()

    ok_cnt = 0
    fail_cnt = 0
    skip_cnt = 0

    # ── 제목 중복은 업로드 SKIP + error/log 기록(상태는 그대로 NEW)
    if dup_by_title:
        now_iso = now_kst().isoformat()
        for dup in dup_by_title:
            _queue_append_error_only(dup["row"], "DUP_TOPIC_TITLE", dup.get("error", ""))
            skip_cnt += 1
            if ws_log:
                try:
                    ws_log.append_row([dup["url"], dup["title"], now_iso, "SKIP", "DUP_TOPIC_TITLE"], value_input_option="RAW")
                except Exception:
                    pass

    # 처리 대상(중복 제거 후)
    candidates = kept if mode_all else kept[:n]

    await update.message.reply_text(
        f"뉴스 카페 업로드 시작: NEW {total_new}건(중복필터 후 {len(kept)}건) 중 {len(candidates)}건 처리합니다. "
        f"(menuId={NAVER_CAFE_NEWS_MENU_ID})"
    )

    # ── 2차: 본문 첫부분 해시 기반 중복(동일 이슈/동일 기사) 필터
    body_hash_len = int(os.getenv("NEWS_DUP_BODY_HASH_CHARS", "550"))
    seen_body_hash = set()

    def _body_hash(text: str) -> str:
        t = (text or "").strip()
        if not t:
            return ""
        snip = t[: max(400, min(body_hash_len, 650))]  # 400~650 사이로 제한
        snip = snip.lower()
        snip = re.sub(r"\s+", " ", snip).strip()
        snip = re.sub(r"\d+", " ", snip)
        snip = re.sub(r"[^0-9A-Za-z가-힣\s]", " ", snip)
        snip = re.sub(r"\s+", " ", snip).strip()
        try:
            import hashlib
            return hashlib.sha1(snip.encode("utf-8", "ignore")).hexdigest()
        except Exception:
            return ""

    # 이미지 삽입(가운데 정렬)용 prefix: 첫 번째 이미지(#0)를 본문 최상단에 넣는다.
    def _image_prefix_html() -> str:
        # style/따옴표를 최소화해 403/999(필터/일시제한) 가능성을 낮춘다.
        # (필요하면 html을 아예 넣지 않고, 첨부 이미지가 상단에 자동 노출되는 방식만 사용해도 됨)
        return "<img src=#0 style=display:block;margin-left:auto;margin-right:auto;max-width:100%;height:auto><br>"


    for it in candidates:
        row_num = it["row"]
        url = it["url"]
        orig_title = it["title"]
        sport = it["sport"]

        # (안전) 이미 로그에 OK로 남아있으면 중복 업로드 방지
        if url in posted_urls:
            posted_at = now_kst().isoformat()
            _queue_update_status(ws_q, row_num, "POSTED", posted_at, "")
            skip_cnt += 1
            continue

        try:
            # 1) 원문 다시 가져오기 + 대표 이미지 추출
            text_body, img_url = fetch_daum_article_text_and_image(url, orig_title=orig_title)
            if not text_body:
                raise ValueError("EMPTY_BODY")

            # 2) 본문 해시 기반 중복 필터(1차 통과 항목들 사이에서만)
            h = _body_hash(text_body)
            if h and (h in seen_body_hash):
                _queue_append_error_only(row_num, "DUP_TOPIC_BODY", it.get("error", ""))
                skip_cnt += 1
                if ws_log:
                    try:
                        ws_log.append_row([url, orig_title, now_kst().isoformat(), "SKIP", "DUP_TOPIC_BODY"], value_input_option="RAW")
                    except Exception:
                        pass
                continue
            if h:
                seen_body_hash.add(h)

            # 3) 완전 재작성
            new_title, rewritten = rewrite_news_full_with_openai(
                text_body,
                orig_title=orig_title or "스포츠 뉴스",
                sport_label=sport or "",
                has_image=bool(img_url),
            )

            # 4) 카페 업로드용 HTML(기존 방식 유지)
            content_html, content_plain = _make_cafe_center_html(rewritten)

            # 5) 이미지 다운로드(가능하면 multipart 업로드) - 실패해도 글은 업로드
            img_bytes, img_name, img_mime = _download_image_bytes(img_url, referer=url)

            posted_at = now_kst().isoformat()
            clubid = NAVER_CAFE_CLUBID
            menuid = NAVER_CAFE_NEWS_MENU_ID  # ✅ 고정 31

            success = False
            info = ""

            # 5-1) 이미지가 있으면: multipart로 여러 변형을 시도 (실패해도 글 업로드는 계속)
            if img_bytes:
                # 1) 가장 보수적인 본문(이미지 태그 없음)으로 multipart 시도
                success, info = _naver_news_cafe_post_multipart(
                    new_title,
                    content_html,
                    clubid,
                    menuid,
                    image_bytes=img_bytes,
                    filename=img_name or "image.jpg",
                    mime_type=img_mime or "image/jpeg",
                )

                # 2) 그래도 실패하면 plain 텍스트로 한 번 더 (필터 회피 목적)
                if not success:
                    print(f"[NEWS_IMAGE] multipart(본문 그대로) 실패 → plain 본문으로 1회 더: {info}")
                    success, info = _naver_news_cafe_post_multipart(
                        new_title,
                        content_plain,
                        clubid,
                        menuid,
                        image_bytes=img_bytes,
                        filename=img_name or "image.jpg",
                        mime_type=img_mime or "image/jpeg",
                    )

                # 3) (옵션) inline(#0) 태그 버전도 1회 더 시도
                if not success:
                    print(f"[NEWS_IMAGE] multipart(plain)도 실패 → inline(#0)로 1회 더: {info}")
                    content_html_img, _ = _make_cafe_center_html(rewritten, raw_prefix_html=_image_prefix_html())
                    success, info = _naver_news_cafe_post_multipart(
                        new_title,
                        content_html_img,
                        clubid,
                        menuid,
                        image_bytes=img_bytes,
                        filename=img_name or "image.jpg",
                        mime_type=img_mime or "image/jpeg",
                    )

                if not success:
                    print(f"[NEWS_IMAGE] 업로드 실패 → 이미지 없이 재시도: {info}")

# 5-2) 이미지 업로드 실패/이미지 없음 → 글만 업로드
            if not success:
                success, info = _naver_news_cafe_post(new_title, content_html, clubid, menuid)

                # HTML에서 999 등이 뜨면 plain 텍스트로 재시도
                if (not success) and ("999" in (info or "")):
                    success, info = _naver_news_cafe_post(new_title, content_plain, clubid, menuid)

            if success:
                _queue_update_status(ws_q, row_num, "POSTED", posted_at, "")
                ok_cnt += 1

                if ws_log:
                    try:
                        ws_log.append_row([url, new_title, posted_at, "OK", ""], value_input_option="RAW")
                    except Exception:
                        pass
                posted_urls.add(url)

            else:
                err = _safe_truncate(info, 300)
                _queue_update_status(ws_q, row_num, "FAIL", "", err)
                fail_cnt += 1

                if ws_log:
                    try:
                        ws_log.append_row([url, orig_title, "", "FAIL", err], value_input_option="RAW")
                    except Exception:
                        pass

                # rate limit이면 잠깐 쉬었다가 계속
                if _news_is_rate_limited(info):
                    await asyncio.sleep(2.0)

            # 요청 간 약간의 텀(과도한 호출 방지)
            await asyncio.sleep(float(os.getenv("CAFE_NEWS_UPLOAD_DELAY_SEC", "7")))

        except Exception as e:
            err = _safe_truncate(f"EXC:{e}", 300)
            _queue_update_status(ws_q, row_num, "FAIL", "", err)
            fail_cnt += 1

            if ws_log:
                try:
                    ws_log.append_row([url, orig_title, "", "FAIL", err], value_input_option="RAW")
                except Exception:
                    pass

            await asyncio.sleep(0.5)

    await update.message.reply_text(f"뉴스 카페 업로드 완료: OK {ok_cnt} / FAIL {fail_cnt} / SKIP {skip_cnt}")




# telegram: ignore 'Message is not modified' when editing inline keyboards
async def _safe_edit_message_reply_markup(q, *args, **kwargs):
    if not q:
        return
    try:
        await q.edit_message_reply_markup(*args, **kwargs)
    except BadRequest as e:
        # Happens when a user taps a button that would not change the keyboard
        if "Message is not modified" in str(e):
            return
        raise

# 4) 인라인 버튼 콜백 처리 (분석/뉴스 팝업)
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        # callback query는 생성 후 짧은 시간 내에 answer 해야 오류가 안 난다.
        try:
            await q.answer()
        except BadRequest as e:
            if "Query is too old" in str(e) or "query id is invalid" in str(e):
                pass
            else:
                raise
    data = q.data or ""
    # 아무 동작 안 하는 더미
    if data == "noop":
        return

    # export 댓글 ZIP 버튼 (1 zip 파일로 전송)
    if data.startswith("zip:"):
        try:
            _, which, sport_key, n = data.split(":", 3)
            which = (which or "tomorrow").strip().lower()
            sport_key = (sport_key or "").strip().lower()
            limit_matches = int(n) if str(n).isdigit() else _default_zip_matches()
        except Exception:
            which, limit_matches, sport_key = "tomorrow", _default_zip_matches(), ""

        await q.message.reply_text(f"📦 댓글 ZIP 생성/전송 시작: {which}, {limit_matches}경기, sport={sport_key or 'ALL'}")
        files_cnt, matches_cnt, zip_name = await _send_export_comment_zip_file(
            chat_id=q.message.chat_id,
            context=context,
            which=which,
            limit_matches=limit_matches,
            sport_filter=sport_key,
        )
        if files_cnt and matches_cnt:
            await q.message.reply_text(f"✅ ZIP 전송 완료: {matches_cnt}경기 / {files_cnt}개 파일 (1 zip)")
        else:
            await q.message.reply_text("ZIP 전송할 댓글이 없거나 실패했습니다.")
        return

    # export 댓글 TXT 버튼
    if data.startswith("txt:"):
        try:
            _, which, sport_key, n = data.split(":", 3)
            which = (which or "tomorrow").strip().lower()
            sport_key = (sport_key or "").strip().lower()
            limit_matches = int(n) if str(n).isdigit() else int(os.getenv("EXPORT_COMMENT_TXT_MATCHES", "10"))
        except Exception:
            which, limit_matches, sport_key = "tomorrow", int(os.getenv("EXPORT_COMMENT_TXT_MATCHES", "10")), ""

        await q.message.reply_text(f"📄 댓글 TXT 생성/전송 시작: {which}, {limit_matches}경기, sport={sport_key or 'ALL'}")
        sent_files, processed = await _send_export_comment_txt_files(
            chat_id=q.message.chat_id,
            context=context,
            which=which,
            limit_matches=limit_matches,
            sport_filter=sport_key,
        )
        await q.message.reply_text(f"✅ TXT 전송 완료: {processed}경기 / {sent_files}개 파일")
        return


    # 메인 메뉴로
    if data == "back_main":
        await _safe_edit_message_reply_markup(q, reply_markup=build_main_inline_menu())
        return

    # 축구 하위 카테고리 (해외축구 / K리그 / J리그)
    if data.startswith("soccer_cat:"):
        _, key, subsport = data.split(":", 2)
        # subsport: "해외축구", "K리그", "J리그"
        await _safe_edit_message_reply_markup(q, 
            reply_markup=build_analysis_match_menu(key, subsport, page=1)
        )
        return

    # 야구 하위 카테고리 (해외야구 / KBO / NPB)
    if data.startswith("baseball_cat:"):
        _, key, subsport = data.split(":", 2)
        # subsport: "해외야구", "KBO", "NPB"
        await _safe_edit_message_reply_markup(q, 
            reply_markup=build_analysis_match_menu(key, subsport, page=1)
        )
        return

        # 농구 하위 카테고리 (NBA / KBL)
    if data.startswith("basket_cat:"):
        _, key, subsport = data.split(":", 2)
        # subsport: "NBA", "KBL"
        await _safe_edit_message_reply_markup(q, 
            reply_markup=build_analysis_match_menu(key, subsport, page=1)
        )
        return

    # 배구 하위 카테고리 (V리그)
    if data.startswith("volley_cat:"):
        _, key, subsport = data.split(":", 2)  # subsport == "V리그"
        await _safe_edit_message_reply_markup(q, 
            reply_markup=build_analysis_match_menu(key, subsport, page=1)
        )
        return
  
    # 종목 선택으로 돌아가기
    if data.startswith("analysis_root:"):
        _, key = data.split(":", 1)
        await _safe_edit_message_reply_markup(q, reply_markup=build_analysis_category_menu(key))
        return

    # 종목 선택 (축구/농구/야구/배구)
    if data.startswith("analysis_cat:"):
        _, key, sport = data.split(":", 2)

        # ⚽ 축구 → 해외축구 / K리그 / J리그 하위 메뉴
        if sport == "축구":
            await _safe_edit_message_reply_markup(q, 
                reply_markup=build_soccer_subcategory_menu(key)
            )
            return

        # ⚾ 야구 → 해외야구 / KBO / NPB 하위 메뉴
        if sport == "야구":
            await _safe_edit_message_reply_markup(q, 
                reply_markup=build_baseball_subcategory_menu(key)
            )
            return

        # 🏀 농구 → NBA / KBL 하위 메뉴
        if sport == "농구":
            await _safe_edit_message_reply_markup(q, 
                reply_markup=build_basketball_subcategory_menu(key)
            )
            return

        # 🏐 배구 → V리그 하위 메뉴
        if sport == "배구":
            await _safe_edit_message_reply_markup(q, 
                reply_markup=build_volleyball_subcategory_menu(key)
            )
            return        

        # 그 외 종목(배구 등)은 바로 경기 리스트 1페이지
        await _safe_edit_message_reply_markup(q, 
            reply_markup=build_analysis_match_menu(key, sport, page=1)
        )
        return
        
    # 경기 리스트 페이지 이동 (이전/다음)
    if data.startswith("match_page:"):
        _, key, sport, page_str = data.split(":", 3)
        try:
            page = int(page_str)
        except ValueError:
            page = 1

        await _safe_edit_message_reply_markup(q, 
            reply_markup=build_analysis_match_menu(key, sport, page=page)
        )
        return

    # 개별 경기 선택
    if data.startswith("match:"):
        _, key, sport, match_id = data.split(":", 3)
        items = ANALYSIS_DATA_MAP.get(key, {}).get(sport, [])

        title = "선택한 경기"
        summary = "해당 경기 분석을 찾을 수 없습니다."

        for item in items:
            if item["id"] == match_id:
                title = item["title"]
                summary = item["summary"]
                break

        text = f"📌 경기 분석 – {title}\n\n{summary}"

        buttons = [
            [InlineKeyboardButton("📺 스포츠 무료 중계", url="https://goat-tv.com")],
            [InlineKeyboardButton("📝 분석글 더 보기", callback_data=f"analysis_root:{key}")],
            [InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")],
        ]

        await q.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        return

    # 뉴스 루트
    if data == "news_root":
        await _safe_edit_message_reply_markup(q, reply_markup=build_news_category_menu())
        return

    # 뉴스 종목 선택
    if data.startswith("news_cat:"):
        sport = data.split(":", 1)[1]
        await _safe_edit_message_reply_markup(q, reply_markup=build_news_list_menu(sport))
        return

    # 뉴스 아이템 선택
    if data.startswith("news_item:"):
        try:
            _, sport, news_id = data.split(":", 2)
            items = NEWS_DATA.get(sport, [])
            title = "뉴스 정보 없음"
            summary = "해당 뉴스 정보를 찾을 수 없습니다."

            for item in items:
                if item["id"] == news_id:
                    title = item["title"]
                    summary = item["summary"]
                    break
        except Exception:
            title = "뉴스 정보 없음"
            summary = "해당 뉴스 정보를 찾을 수 없습니다."

        text = f"📰 뉴스 요약 – {title}\n\n{summary}"

        buttons = [
            [InlineKeyboardButton("📺 스포츠무료중계", url="https://goat-tv.com")],
            [InlineKeyboardButton("📰 다른 뉴스 보기", callback_data="news_root")],
            [InlineKeyboardButton("◀ 메인 메뉴로", callback_data="back_main")],
        ]

        await q.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

async def crawlmazsoccer_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1) 해외축구
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/overseas",
        sport_label="축구",
        league_default="해외축구",
        day_key="tomorrow",
        max_pages=5,
        board_type=2,
        category=1,
        export_site=True,   # ✅ 추가
    )

    # 2) K리그/J리그(asia)
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/asia",
        sport_label="축구",
        league_default="K리그/J리그",
        day_key="tomorrow",
        max_pages=5,
        board_type=2,
        category=2,
        export_site=True,   # ✅ 추가
    )

    await update.message.reply_text(
        "⚽ 텔레그램용 + 사이트용(내일) 분석 크롤링을 모두 저장했습니다.",
        reply_markup=_build_export_comment_zip_markup("tomorrow", "soccer"),
    )


# 야구(MLB · KBO · NPB) 분석 (내일 경기 → tomorrow 시트)
async def crawlmazbaseball_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    mazgtv 야구(MLB / KBO / NPB) 내일 경기 분석을 크롤링해서
    'tomorrow' 시트에 저장한다. 축구용과 동일한 구조.
    """
    # 해외야구(MLB)
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/mlb",
        sport_label="야구",
        league_default="해외야구",
        day_key="tomorrow",
        max_pages=5,
        board_type=2,
        category=3,
        export_site=True,
    )

    # KBO + NPB
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/baseball",
        sport_label="야구",
        league_default="KBO/NPB",
        day_key="tomorrow",
        max_pages=5,
        board_type=2,
        category=4,
        export_site=True,
    )

    await update.message.reply_text(
        "⚾ 야구(MLB · KBO · NPB) 내일 경기 분석 크롤링 명령을 모두 실행했습니다.",
        reply_markup=_build_export_comment_zip_markup("tomorrow", "baseball"),
    )

# 🔹 NBA + 국내 농구/배구 (내일 경기) 크롤링
async def bvcrawl_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    mazgtv 농구/배구 분석:
    - NBA 분석:    https://mazgtv1.com/analyze/nba
    - 국내 농구/배구: https://mazgtv1.com/analyze/volleyball
    두 곳에서 '내일 경기' 분석글을 크롤링해서 tomorrow 시트에 저장한다.
    """

    # 1) NBA (해외 농구)
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/nba",
        sport_label="농구",          # 시트에는 NBA/KBL/WKBL 등으로 나뉨
        league_default="NBA",
        day_key="tomorrow",
        max_pages=5,
        board_type=2,                # ⚠️ 실제 boardType 값으로 수정 필요
        category=5,                  # ⚠️ 실제 category 값으로 수정 필요
        # target_ymd=None → 자동으로 '내일' 날짜 사용
        export_site=True,
    )

    # 2) 국내 농구 + 배구 (KBL / WKBL / V리그 등)
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/volleyball",
        sport_label="농구/배구",     # 분류 함수에서 KBL/WKBL/V리그/배구 등으로 세분화
        league_default="국내농구/배구",
        day_key="tomorrow",
        max_pages=5,
        board_type=2,                # ⚠️ 실제 boardType 값으로 수정 필요
        category=7,                  # ⚠️ 실제 category 값으로 수정 필요
        export_site=True,
    )

    await update.message.reply_text(
        "NBA + 국내 농구/배구(내일 경기) 분석 크롤링을 모두 실행했습니다.\n"
        "/syncsheet 로 텔레그램 메뉴 데이터를 갱신할 수 있습니다.",
        reply_markup=_build_export_comment_zip_markup_bv("tomorrow"),
    )

async def crawlmazsoccer_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    mazgtv 해외축구 + K리그/J리그 분석 중
    '오늘 날짜' 경기를 크롤링해서 today 시트에 저장.
    """

    # 1) 해외축구 탭
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/overseas",
        sport_label="축구",          # 안에서 '해외축구/K리그/J리그'로 다시 분류됨
        league_default="해외축구",
        day_key="today",            # ✅ today
        max_pages=5,
        board_type=2,
        category=1,                 # 해외축구
        export_site=True,
    )

    # 2) K리그 / J리그 탭
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/asia",
        sport_label="축구",
        league_default="K리그/J리그",
        day_key="today",            # ✅ today
        max_pages=5,
        board_type=2,
        category=2,                 # K리그/J리그
        export_site=True,
    )

    await update.message.reply_text(
        "⚽ 해외축구 + K리그/J리그 오늘 경기 분석 크롤링을 모두 실행했습니다.",
        reply_markup=_build_export_comment_zip_markup("today", "soccer"),
    )

async def crawlmazbaseball_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    mazgtv 야구 분석(MLB + KBO + NPB) 중
    '오늘 날짜' 경기를 크롤링해서 today 시트에 저장.
    """

    # 1) 해외야구 (MLB)
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/mlb",
        sport_label="야구",          # 시트에서는 해외야구/KBO/NPB로 분리됨
        league_default="해외야구",
        day_key="today",            # 🔴 오늘
        max_pages=5,
        board_type=2,               # 기존 /crawlmazbaseball_tomorrow 와 동일
        category=3,                 # MLB 쪽 category 값 (지금 쓰는 값 그대로)
        export_site=True,
    )

    # 2) KBO + NPB
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/baseball",
        sport_label="야구",
        league_default="KBO/NPB",
        day_key="today",            # 🔴 오늘
        max_pages=5,
        board_type=2,               # 동일 boardType
        category=4,                 # KBO/NPB 쪽 category 값 (지금 쓰는 값 그대로)
        export_site=True,
    )

    await update.message.reply_text(
        "⚾ mazgtv 야구(MLB · KBO · NPB) '오늘 경기' 분석 크롤링을 완료했습니다.\n"
        "today 시트에서 내용을 확인할 수 있습니다.",
        reply_markup=_build_export_comment_zip_markup("today", "baseball"),
    )

# 🔹 NBA + 국내 농구/배구 (오늘 경기) 크롤링
async def bvcrawl_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    mazgtv 농구/배구 분석:
    - NBA 분석:    https://mazgtv1.com/analyze/nba
    - 국내 농구/배구: https://mazgtv1.com/analyze/volleyball
    두 곳에서 '오늘 경기' 분석글을 크롤링해서 today 시트에 저장한다.
    """

    # 1) NBA (해외 농구)
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/nba",
        sport_label="농구",
        league_default="NBA",
        day_key="today",             # ✅ 오늘
        max_pages=5,
        board_type=2,                # 👉 tomorrow와 동일 값 유지
        category=5,
        export_site=True,
    )

    # 2) 국내 농구 + 배구 (KBL / WKBL / V리그 등)
    await crawl_maz_analysis_common(
        update,
        context,
        base_url="https://mazgtv1.com/analyze/volleyball",
        sport_label="농구/배구",
        league_default="국내농구/배구",
        day_key="today",             # ✅ 오늘
        max_pages=5,
        board_type=2,                # 👉 tomorrow와 동일 값 유지
        category=7,
        export_site=True,
    )

    await update.message.reply_text(
        "NBA + 국내 농구/배구(오늘 경기) 분석 크롤링을 모두 실행했습니다.\n"
        "today 시트에서 내용을 확인할 수 있습니다.",
        reply_markup=_build_export_comment_zip_markup_bv("today"),
    )


# ───────────────── 실행부 ─────────────────

async def export_rollover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """export_tomorrow → export_today 롤오버 (덮어쓰기).
    - export_today 기존 데이터는 모두 비우고(헤더 재설정) export_tomorrow 데이터를 그대로 복사
    - 이후 export_tomorrow는 헤더만 남김
    """
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    today_ws = get_export_ws(EXPORT_TODAY_SHEET_NAME)
    tomo_ws = get_export_ws(EXPORT_TOMORROW_SHEET_NAME)
    if not today_ws or not tomo_ws:
        await update.message.reply_text("export 시트 준비에 실패했습니다. 구글시트 설정을 확인하세요.")
        return

    try:
        tomo_vals = tomo_ws.get_all_values()
        if not tomo_vals or len(tomo_vals) <= 1:
            await update.message.reply_text("export_tomorrow에 옮길 데이터가 없습니다.")
            return

        # 옮길 데이터(헤더 제외). src_id가 비어있는 행은 제외.
        header = [c.strip() for c in (tomo_vals[0] if tomo_vals else EXPORT_HEADER)]
        src_idx = header.index("src_id") if "src_id" in header else 2

        to_move = []
        for r in tomo_vals[1:]:
            sid = r[src_idx].strip() if len(r) > src_idx else ""
            if not sid:
                continue
            to_move.append(r)

        # export_today는 덮어쓰기: 전체 비우고 헤더 재설정 후, 내일 데이터를 그대로 넣는다.
        try:
            today_ws.clear()
        except Exception:
            try:
                today_ws.batch_clear(["A2:Z"])
            except Exception:
                pass

        try:
            today_ws.update(range_name="A1", values=[EXPORT_HEADER])
        except Exception:
            today_ws.update(values=[EXPORT_HEADER], range_name="A1")

        if to_move:
            today_ws.append_rows(to_move, value_input_option="RAW", table_range="A1")

        # export_tomorrow 초기화(헤더만)
        tomo_ws.clear()
        tomo_ws.update(range_name="A1", values=[EXPORT_HEADER])

        await update.message.reply_text(
            f"롤오버 완료(덮어쓰기): export_today에 {len(to_move)}건 반영, export_tomorrow 초기화 완료."
        )

    except Exception as e:
        await update.message.reply_text(f"롤오버 중 오류: {e}")
        return

# ───────────────── 네이버 카페(웹 API) 게시글 수집 → youtoo 시트 저장 ─────────────────
# ⚠️ 주의: 아래 API는 '카페 웹'에서 사용하는 내부 API 성격이라, 스펙/정책이 바뀔 수 있습니다.
# - 필요 환경변수:
#   - NAVER_COOKIE : 브라우저에서 로그인한 뒤 얻은 쿠키 문자열(예: "NID_AUT=...; NID_SES=...; ...;")
#   - SPREADSHEET_ID, GOOGLE_SERVICE_KEY : 구글시트 저장용(기존 설정 그대로)
#
# - 선택 환경변수(기본값은 seane01/18677861/menu=20):
#   - NAVER_CAFE_BASE_URL (default: https://cafe.naver.com/seane01)
#   - NAVER_CAFE_WEB_CAFE_ID (default: 18677861)
#   - NAVER_CAFE_WEB_MENU_ID (default: 20)

NAVER_CAFE_BASE_URL = (os.getenv("NAVER_CAFE_BASE_URL") or "https://cafe.naver.com/seane01").strip().rstrip("/")
NAVER_CAFE_WEB_CAFE_ID = (os.getenv("NAVER_CAFE_WEB_CAFE_ID") or "18677861").strip()
NAVER_CAFE_WEB_MENU_ID = (os.getenv("NAVER_CAFE_WEB_MENU_ID") or "20").strip()
NAVER_CAFE_WEB_SORT_BY = (os.getenv("NAVER_CAFE_WEB_SORT_BY") or "TIME").strip()
NAVER_CAFE_WEB_VIEW_TYPE = (os.getenv("NAVER_CAFE_WEB_VIEW_TYPE") or "L").strip()
# 댓글 목록 API(웹 내부). 환경변수로 템플릿을 지정하면 가장 안정적이다.
# - NAVER_CAFE_COMMENT_URL_TEMPLATE 예)
#   https://apis.naver.com/cafe-web/cafe-comment-api/v1/cafes/{cafeId}/articles/{articleId}/comments?page=1&pageSize=20&sortBy=TIME
# 댓글 목록 API(웹 내부). 가장 안정적인 건 브라우저(Network)에서 확인한 "댓글 목록" Request URL 패턴을
# 템플릿으로 지정하는 것이다.
#
# ✅ 2026-02 기준 댓글 목록 URL 패턴(예):
#   https://article.cafe.naver.com/gw/v4/cafes/{cafeId}/articles/{articleId}/comments/pages/1
#
# 위 URL 끝에 querystring이 붙는 경우가 있는데(브라우저 Network의 Request URL에서 '?...' 부분),
# Render 환경변수 NAVER_CAFE_COMMENT_QUERYSTRING 에 '?' 뒤의 문자열을 그대로 넣으면 된다.
#
# - NAVER_CAFE_COMMENT_URL_TEMPLATE: 댓글 목록 URL 템플릿(권장)
# - NAVER_CAFE_COMMENT_QUERYSTRING: 댓글 목록 URL에 붙는 쿼리스트링(선택)
NAVER_CAFE_COMMENT_URL_TEMPLATE = (os.getenv("NAVER_CAFE_COMMENT_URL_TEMPLATE") or "").strip()
NAVER_CAFE_COMMENT_QUERYSTRING = (
    (os.getenv("NAVER_CAFE_COMMENT_QUERYSTRING") or "").strip()
    or (os.getenv("NAVER_CAFE_COMMENT_QS") or "").strip()
)

def _normalize_qs(q: str) -> str:
    q = (q or "").strip()
    if not q:
        return ""
    if q.startswith("?"):
        q = q[1:].strip()
    return q

def _append_qs(url: str, q: str) -> str:
    q = _normalize_qs(q)
    if not q:
        return url
    if "?" in url:
        # 이미 쿼리가 있으면 '&'로 이어붙인다.
        if url.endswith("?") or url.endswith("&"):
            return f"{url}{q}"
        return f"{url}&{q}"
    return f"{url}?{q}"

def _build_comment_url_candidates(cafe_id: str, article_id: str) -> list[str]:
    """댓글 목록 API 후보 URL 리스트.

    ✅ 2026-02 기준: 댓글 목록은 `article.cafe.naver.com/gw/v4/.../comments/pages/1` 형태로 내려오는 경우가 많다.
    - 가장 확실한 방법: 브라우저 Network에서 확인한 URL 패턴을 NAVER_CAFE_COMMENT_URL_TEMPLATE로 지정
    - URL에 querystring이 붙는 경우: NAVER_CAFE_COMMENT_QUERYSTRING에 '?' 뒤 문자열을 지정
    """
    candidates: list[str] = []

    # 1) 사용자 지정 템플릿(가장 안정)
    if NAVER_CAFE_COMMENT_URL_TEMPLATE:
        u = NAVER_CAFE_COMMENT_URL_TEMPLATE.format(
            cafeId=cafe_id,
            articleId=article_id,
            cafe_id=cafe_id,
            article_id=article_id,
        )
        if NAVER_CAFE_COMMENT_QUERYSTRING and "?" not in u:
            candidates.append(_append_qs(u, NAVER_CAFE_COMMENT_QUERYSTRING))
        candidates.append(u)

        # 중복 제거(순서 유지)
        out: list[str] = []
        seen: set[str] = set()
        for x in candidates:
            if x and x not in seen:
                out.append(x)
                seen.add(x)
        return out

    # 2) 기본(추천) 패턴: article.cafe gateway
    base_article = f"https://article.cafe.naver.com/gw/v4/cafes/{cafe_id}/articles/{article_id}/comments/pages/1"
    qs_options: list[str] = []
    if NAVER_CAFE_COMMENT_QUERYSTRING:
        qs_options.append(NAVER_CAFE_COMMENT_QUERYSTRING)
    qs_options.append("")  # 쿼리 없이도 되는 경우가 있어 먼저 시도

    # 흔한 후보(필수는 아님 - 네이버가 요구하는 파라미터가 있으면 env로 지정 권장)
    qs_options.extend([
        "pageSize=20",
        "pageSize=50",
        "pageSize=20&sortBy=TIME",
        "pageSize=50&sortBy=TIME",
    ])

    for q in qs_options:
        candidates.append(_append_qs(base_article, q) if q else base_article)

    # 3) 과거/대체 패턴(cafe-web) - fallback
    base = "https://apis.naver.com/cafe-web"
    candidates.extend([
        f"{base}/cafe-comment-api/v1/cafes/{cafe_id}/articles/{article_id}/comments?page=1&pageSize=20&sortBy=TIME",
        f"{base}/cafe-comment-api/v2/cafes/{cafe_id}/articles/{article_id}/comments?page=1&pageSize=20&sortBy=TIME",
        f"{base}/cafe-comment-api/v3/cafes/{cafe_id}/articles/{article_id}/comments?page=1&pageSize=20&sortBy=TIME",
        f"{base}/cafe-comment-api/v1/cafes/{cafe_id}/articles/{article_id}/best-comments?page=1&pageSize=20",
    ])

    # 중복 제거(순서 유지)
    out: list[str] = []
    seen: set[str] = set()
    for x in candidates:
        if x and x not in seen:
            out.append(x)
            seen.add(x)
    return out

def _get_naver_web_cookie() -> str:
    """Render 환경변수에 저장된 쿠키 문자열을 가져온다.
    지원 키: NAVER_COOKIE > NAVER_CAFE_COOKIE > NAVER_WEB_COOKIE
    """
    ck = (
        os.getenv("NAVER_COOKIE")
        or os.getenv("NAVER_CAFE_COOKIE")
        or os.getenv("NAVER_WEB_COOKIE")
        or ""
    )
    ck = str(ck).strip()

    # Render 환경변수에 따옴표로 감싸 넣은 경우 제거
    if (ck.startswith('"') and ck.endswith('"')) or (ck.startswith("'") and ck.endswith("'")):
        ck = ck[1:-1].strip()

    if ck.lower().startswith("cookie:"):
        ck = ck.split(":", 1)[1].strip()

    # 줄바꿈/연속 공백 제거
    ck = " ".join(ck.splitlines()).strip()
    return ck

def _naver_web_headers(cafe_id: str, menu_id: str) -> dict[str, str]:
    cookie = _get_naver_web_cookie()
    ua = (
        (os.getenv("NAVER_USER_AGENT") or "").strip()
        or (os.getenv("MAZ_USER_AGENT") or "").strip()
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )

    # Referer는 엄격히 체크되는 경우가 있어 '게시판 목록' 페이지 형태로 맞춰준다.
    referer = (os.getenv("NAVER_CAFE_REFERER") or "").strip()
    if not referer:
        referer = f"{NAVER_CAFE_BASE_URL}/ArticleList.nhn?search.clubid={cafe_id}&search.menuid={menu_id}"

    headers = {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": referer,
        "Origin": "https://cafe.naver.com",
        "Connection": "keep-alive",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers

def _ms_to_kst_str(ts_ms) -> str:
    """네이버 writeDateTimestamp(ms 또는 sec)를 KST 문자열로 변환."""
    try:
        n = int(ts_ms)
    except Exception:
        return ""
    # 13자리면 ms로 판단
    if n > 10**12:
        sec = n / 1000.0
    else:
        sec = float(n)
    try:
        dt = datetime.fromtimestamp(sec, tz=timezone.utc).astimezone(KST)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""

async def _fetch_cafe_boardlist_page(
    client: httpx.AsyncClient,
    *,
    cafe_id: str,
    menu_id: str,
    page: int,
    page_size: int,
    sort_by: str,
    view_type: str,
) -> tuple[int, dict | None, str]:
    """(status_code, json_or_none, text_snippet)"""
    url = f"https://apis.naver.com/cafe-web/cafe-boardlist-api/v1/cafes/{cafe_id}/menus/{menu_id}/articles"
    params = {
        "page": max(1, int(page)),
        "pageSize": max(1, int(page_size)),
        "sortBy": (sort_by or "TIME"),
        "viewType": (view_type or "L"),
    }
    headers = _naver_web_headers(cafe_id, menu_id)
    r = await client.get(url, params=params, headers=headers, timeout=20.0)
    snippet = (r.text or "")[:500]
    if not (200 <= r.status_code < 300):
        return r.status_code, None, snippet
    try:
        return r.status_code, r.json(), snippet
    except Exception:
        return r.status_code, None, snippet


async def _fetch_first_comment(
    client: httpx.AsyncClient,
    *,
    cafe_id: str,
    article_id: str,
    cookie: str,
) -> tuple[str, str, str]:
    """해당 게시글의 '첫 댓글'을 가져온다.

    반환: (content, nick, time_kst_str)
    - 댓글이 없거나 실패하면 ("", "", "") 반환
    """
    # 댓글이 없을 때는 굳이 호출하지 않도록 youtoo()에서 commentCount로 1차 거름.
    ua = (
        (os.getenv("NAVER_USER_AGENT") or "").strip()
        or (os.getenv("MAZ_USER_AGENT") or "").strip()
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    headers = {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
        "Cookie": cookie,
        # Referer/Origin을 넣어야 정상 응답이 오는 경우가 많다.
        "Referer": f"{NAVER_CAFE_BASE_URL}/{article_id}",
        "Origin": "https://cafe.naver.com",
        "X-Requested-With": "XMLHttpRequest",
    }

    for url in _build_comment_url_candidates(cafe_id, str(article_id)):
        try:
            r = await client.get(url, headers=headers, timeout=15.0)
        except Exception:
            continue

        if r.status_code in (401, 403):
            # 쿠키/권한 문제
            return ("", "", "")

        if not (200 <= r.status_code < 300):
            continue

        try:
            data = r.json()
        except Exception:
            continue

        # 기대 구조: {"comments": {"items": [ ... ]}}
        comments = data.get("comments") if isinstance(data, dict) else None
        items = (comments or {}).get("items") if isinstance(comments, dict) else None
        if not isinstance(items, list) or not items:
            # 다른 응답 스키마 가능성 대비: {"result":{"comments":{"items":[]}}}
            result = data.get("result") if isinstance(data, dict) else None
            comments = (result or {}).get("comments") if isinstance(result, dict) else None
            items = (comments or {}).get("items") if isinstance(comments, dict) else None

        if not isinstance(items, list) or not items:
            continue

        first = items[0] if isinstance(items[0], dict) else None
        if not isinstance(first, dict):
            continue

        content = str(first.get("content") or "").strip()
        writer = first.get("writer") if isinstance(first.get("writer"), dict) else {}
        nick = str((writer or {}).get("nick") or "").strip()
        t = _ms_to_kst_str(first.get("updateDate"))
        return (content, nick, t)

    return ("", "", "")
async def youtoo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/youtoo 명령어:
    네이버 카페(기본: 18677861) 메뉴(기본: 20) 글 목록을 가져와 구글시트 youtoo 탭에 저장한다.

    사용 예)
    - /youtoo                -> 1페이지(page=1) 15개 저장
    - /youtoo 3              -> 1~3페이지까지 저장
    - /youtoo 3 30           -> 1~3페이지, 페이지당 30개
    - /youtoo page=2         -> 2페이지(1페이지만)
    - /youtoo page=2 pages=2 -> 2~3페이지
    - /youtoo menu=20        -> 메뉴ID를 바꿔서 수집(테스트/확장용)
    """
    if not is_admin(update):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    cookie = _get_naver_web_cookie()
    if not cookie:
        await update.message.reply_text(
            "NAVER_COOKIE 환경변수가 비어있습니다.\n"
            "Render 환경변수에 브라우저 쿠키 문자열을 넣어주세요."
        )
        return

    # 기본값
    start_page = 1
    pages = 1
    page_size = 15

    # args 파싱(키=값 우선)
    raw_args = list(context.args or [])
    kv = {}
    nums = []
    for a in raw_args:
        s = (a or "").strip()
        if not s:
            continue
        if "=" in s:
            k, v = s.split("=", 1)
            kv[k.strip().lower()] = v.strip()
        elif s.isdigit():
            nums.append(int(s))

    def _safe_int(v, default: int) -> int:
        try:
            return int(str(v).strip())
        except Exception:
            return default

    # 키=값 우선 처리
    if "page" in kv or "p" in kv:
        start_page = _safe_int(kv.get("page") or kv.get("p"), 1)
    if "pages" in kv or "n" in kv:
        pages = _safe_int(kv.get("pages") or kv.get("n"), 1)
    if "size" in kv or "pagesize" in kv or "ps" in kv:
        page_size = _safe_int(kv.get("size") or kv.get("pagesize") or kv.get("ps"), 15)

    # 숫자 인자 처리 (/youtoo 3 30 형태)
    if nums:
        pages = nums[0]
        if len(nums) >= 2:
            page_size = nums[1]

    # 안전 범위
    start_page = max(1, start_page)
    pages = max(1, min(20, pages))          # 너무 많이 가져오면 차단/시간초과 위험
    page_size = max(1, min(50, page_size))  # 일반적으로 50이면 충분

    cafe_id = str(kv.get("cafe") or kv.get("cafeid") or NAVER_CAFE_WEB_CAFE_ID).strip() or NAVER_CAFE_WEB_CAFE_ID
    menu_id = str(kv.get("menu") or kv.get("menuid") or NAVER_CAFE_WEB_MENU_ID).strip() or NAVER_CAFE_WEB_MENU_ID

    await update.message.reply_text(
        f"네이버 카페 게시글을 가져옵니다. (cafeId={cafe_id}, menuId={menu_id})\n"
        f"- page={start_page} ~ {start_page + pages - 1}\n"
        f"- pageSize={page_size}\n"
        f"잠시만 기다려 주세요..."
    )

    ws = get_youtoo_ws()
    if not ws:
        await update.message.reply_text("구글시트(youtoo 탭) 준비에 실패했습니다. SPREADSHEET_ID/권한을 확인하세요.")
        return

    new_rows: list[list[str]] = []
    seen: set[str] = set()

    sort_by = NAVER_CAFE_WEB_SORT_BY
    view_type = NAVER_CAFE_WEB_VIEW_TYPE

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            for p in range(start_page, start_page + pages):
                status, data, snippet = await _fetch_cafe_boardlist_page(
                    client,
                    cafe_id=cafe_id,
                    menu_id=menu_id,
                    page=p,
                    page_size=page_size,
                    sort_by=sort_by,
                    view_type=view_type,
                )

                # 쿠키/권한 문제 감지
                if status in (401, 403):
                    await update.message.reply_text(
                        f"접근이 거부되었습니다. (HTTP {status})\n"
                        "쿠키가 만료되었거나(또는 해당 게시판을 읽을 권한이 없는 계정)일 수 있습니다.\n"
                        "브라우저에서 해당 계정으로 다시 로그인한 뒤 쿠키를 갱신해서 Render 환경변수(NAVER_COOKIE)를 업데이트해주세요."
                    )
                    return

                if not data:
                    # 200인데 JSON 파싱 실패 등
                    await update.message.reply_text(
                        f"page={p} 응답 파싱에 실패했습니다.\n"
                        f"(HTTP {status}) 응답 일부: {snippet}"
                    )
                    return

                result = data.get("result") if isinstance(data, dict) else None
                article_list = (result or {}).get("articleList") if isinstance(result, dict) else None
                if not isinstance(article_list, list) or not article_list:
                    # 더 이상 글이 없으면 중단
                    break

                for entry in article_list:
                    if not isinstance(entry, dict):
                        continue
                    item = entry.get("item")
                    if not isinstance(item, dict):
                        continue

                    article_id = item.get("articleId")
                    if not article_id:
                        continue

                    src_id = f"navercafe:{cafe_id}:{menu_id}:{article_id}"
                    # 동일 실행 내 중복만 방지(시트에 이미 있어도 '업데이트' 대상이므로 스킵하지 않음)
                    if src_id in seen:
                        continue
                    seen.add(src_id)

                    subject = str(item.get("subject") or "").strip()
                    comment_cnt = item.get("commentCount")
                    if comment_cnt is None:
                        comment_cnt = item.get("replyArticleCount")
                    comment_cnt = str(comment_cnt or 0)

                    writer = item.get("writerInfo") if isinstance(item.get("writerInfo"), dict) else {}
                    nick = str((writer or {}).get("nickName") or "").strip()

                    posted_at = _ms_to_kst_str(item.get("writeDateTimestamp"))
                    read_cnt = str(item.get("readCount") or 0)
                    like_cnt = str(item.get("likeCount") or 0)


                    # 첫 댓글(있을 때만 추가 호출)
                    first_comment = ""
                    first_comment_nick = ""
                    first_comment_time = ""
                    try:
                        _cc = int(str(comment_cnt))
                    except Exception:
                        _cc = 0
                    if _cc > 0:
                        first_comment, first_comment_nick, first_comment_time = await _fetch_first_comment(
                            client,
                            cafe_id=cafe_id,
                            article_id=str(article_id),
                            cookie=cookie,
                        )

                    # 본문 링크(카페 별칭 URL 기반)
                    link = f"{NAVER_CAFE_BASE_URL}/{article_id}"

                    new_rows.append([src_id, subject, comment_cnt, read_cnt, like_cnt, link, first_comment, first_comment_nick, first_comment_time, posted_at, nick])

        # 시트 저장 (✅ 신규는 상단 삽입 / 기존은 덮어쓰기)
        inserted = 0
        updated = 0
        if new_rows:
            ok, inserted, updated = upsert_youtoo_rows_top(new_rows)
            if not ok:
                await update.message.reply_text("구글시트(youtoo)에 저장하지 못했습니다. 권한/시트 상태를 확인하세요.")
                return

        await update.message.reply_text(
            f"✅ youtoo 수집 완료\n"
            f"- 신규 추가(상단): {inserted}건\n"
            f"- 기존 업데이트: {updated}건\n"
            f"- 수집: {len(new_rows)}건\n"
            f"- 대상 페이지: {start_page}~{start_page + pages - 1} (pageSize={page_size})"
        )

    except Exception as e:
        await update.message.reply_text(f"요청 중 오류가 발생했습니다: {e}")
        return





async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """전역 에러 핸들러: 예외를 로그로 남기고(가능하면) 호출자에게 간단히 안내한다.

    - Render 로그를 놓쳤을 때, 텔레그램에서 최소한 '에러가 났다'는 사실을 알 수 있게 함
    - 민감정보(쿠키/토큰)가 메시지에 포함되지 않도록 예외 메시지는 짧게 잘라서 전송
    """
    try:
        import traceback
        traceback.print_exception(context.error)
    except Exception:
        pass

    try:
        # update가 Update일 수도 있고 아닐 수도 있어 안전하게 처리
        if update and getattr(update, "effective_chat", None) and getattr(update, "message", None):
            msg = str(getattr(context, "error", ""))
            msg = msg.replace("\n", " ").replace("\r", " ")
            # 쿠키/토큰 문자열이 섞였을 가능성 대비해 길이 제한
            if len(msg) > 400:
                msg = msg[:400] + "…"
            await update.message.reply_text(
                "⚠️ 처리 중 오류가 발생했습니다. Render 로그를 확인해주세요.\n"
                f"에러: {msg}"
            )
    except Exception:
        # 에러 핸들러에서 또 에러나면 조용히 무시
        return

def main():
    reload_analysis_from_sheet()
    reload_news_from_sheet()

    app = ApplicationBuilder().token(TOKEN).build()

    # 전역 에러 핸들러 등록(예외 발생 시 최소 안내)
    app.add_error_handler(_on_error)

    # 모든 업데이트에 대해 update_id 중복 처리 방지(웹훅 재전송/슬립 복귀 시 중복 응답 방지)
    app.add_handler(TypeHandler(Update, _dedup_update_guard), group=-1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_handler(CommandHandler("publish", publish))
    app.add_handler(CommandHandler("syncsheet", syncsheet))
    # 뉴스 시트 전체 초기화
    app.add_handler(CommandHandler("newsclean", newsclean))
    # today / tomorrow / news 전체 초기화
    app.add_handler(CommandHandler("allclean", allclean))

    # export_tomorrow → export_today 롤오버
    app.add_handler(CommandHandler("export_rollover", export_rollover))
    app.add_handler(CommandHandler("export_comment_fill", export_comment_fill))    
    app.add_handler(CommandHandler("export_comment_txt", export_comment_txt))
    app.add_handler(CommandHandler("export_comment_zip", export_comment_zip))
    app.add_handler(CommandHandler("export_comment_zip_buttons", export_comment_zip_buttons))
    app.add_handler(CommandHandler("youtoo", youtoo))  # 네이버 카페 메뉴 글 수집 → youtoo 시트

    # 네이버 카페 자동 글쓰기(종목별 게시판)  ※ /cafe_soccer [tomorrow] 처럼 사용
    app.add_handler(CommandHandler("cafe_soccer", cafe_soccer))
    app.add_handler(CommandHandler("cafe_baseball", cafe_baseball))
    app.add_handler(CommandHandler("cafe_basketball", cafe_basketball))
    app.add_handler(CommandHandler("cafe_volleyball", cafe_volleyball))

    # 네이버 카페 자동 글쓰기(심층 분석 게시판)  ※ /cafe_soccer_deep [tomorrow]
    app.add_handler(CommandHandler("cafe_soccer_deep", cafe_soccer_deep))
    app.add_handler(CommandHandler("cafe_baseball_deep", cafe_baseball_deep))
    app.add_handler(CommandHandler("cafe_basketball_deep", cafe_basketball_deep))
    app.add_handler(CommandHandler("cafe_volleyball_deep", cafe_volleyball_deep))

    # 뉴스 큐 → 네이버 카페 업로드 (menuId=31, 뉴스용 토큰)
    app.add_handler(CommandHandler("cafe_news_upload", cafe_news_upload))

    # 네이버 카페 자동 글쓰기    # 분석 시트 부분 초기화 명령어들 (모두 tomorrow 시트 기준)
    app.add_handler(CommandHandler("soccerclean", soccerclean))
    app.add_handler(CommandHandler("baseballclean", baseballclean))
    app.add_handler(CommandHandler("basketclean", basketclean))
    app.add_handler(CommandHandler("volleyclean", volleyclean))
    app.add_handler(CommandHandler("etcclean", etcclean))
    app.add_handler(CommandHandler("analysisclean", analysisclean))

    app.add_handler(CommandHandler("rollover", rollover))

    # 뉴스 크롤링 명령어들 (Daum)
    app.add_handler(CommandHandler("crawlsoccer", crawlsoccer))             # 해외축구
    app.add_handler(CommandHandler("crawlsoccerkr", crawlsoccerkr))         # 국내축구
    app.add_handler(CommandHandler("crawlbaseball", crawlbaseball))         # KBO
    app.add_handler(CommandHandler("crawloverbaseball", crawloverbaseball)) # 해외야구
    app.add_handler(CommandHandler("crawlbasketball", crawlbasketball))     # 농구
    app.add_handler(CommandHandler("crawlvolleyball", crawlvolleyball))     # 배구

    # mazgtv 해외축구 분석 (오늘 / 내일 경기 → today / tomorrow 시트)
    app.add_handler(CommandHandler("crawlmazsoccer_today", crawlmazsoccer_today))
    app.add_handler(CommandHandler("crawlmazsoccer_tomorrow", crawlmazsoccer_tomorrow))

    # mazgtv 야구 분석 (오늘 / 내일)
    app.add_handler(CommandHandler("crawlmazbaseball_today", crawlmazbaseball_today))
    app.add_handler(CommandHandler("crawlmazbaseball_tomorrow", crawlmazbaseball_tomorrow))

    # mazgtv 농구 + 배구 분석 (오늘 / 내일)
    app.add_handler(CommandHandler("bvcrawl_today", bvcrawl_today))
    app.add_handler(CommandHandler("bvcrawl_tomorrow", bvcrawl_tomorrow))




    app.add_handler(CallbackQueryHandler(on_callback))

    port = int(os.environ.get("PORT", "10000"))
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=WEBHOOK_PATH,
        webhook_url=f"{APP_URL}/{WEBHOOK_PATH}",
    )


if __name__ == "__main__":
    main()









