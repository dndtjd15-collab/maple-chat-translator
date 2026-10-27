# -*- coding: utf-8 -*-
"""
메이플 채팅 번역기 (MapleStory Classic World chat translator)

채팅창 영역을 주기적으로 캡처 -> 윈도우 기본 OCR로 글자 인식
-> 메이플 용어 사전으로 줄임말/아이템 용어 처리 -> 구글 번역(무료)
-> 화면 위 오버레이에 한국어로 표시.

단축키: F9 = 채팅창 영역 선택, F8 = 번역 시작/정지 (오버레이 버튼으로도 가능)
테스트: python maple_translator.py --test "S> Wand MA 60% 175k obo, gz!"
"""
import csv
import json
import os
import queue
import re
import sys
import threading
import time
from difflib import SequenceMatcher

APP_VERSION = 71  # 업데이트할 때마다 1씩 올림 (GitHub version.json 과 비교)
UPDATE_BASE = "https://raw.githubusercontent.com/dndtjd15-collab/maple-chat-translator/main/"

APP_DIR = os.path.dirname(os.path.abspath(__file__))
GLOSSARY_PATH = os.path.join(APP_DIR, "maple_glossary.json")
AI_MEMORY_PATH = os.path.join(APP_DIR, "ai_memory.json")  # AI 번역 저장소 (같은 채팅이 또 오면 재사용)
MY_GLOSSARY_PATH = os.path.join(APP_DIR, "my_glossary.json")  # 성윤님 개인 사전 (업데이트로 안 지워짐)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULT_CONFIG = {
    "ocr_lang": "en",          # 윈도우 OCR 언어 (en 추천, 한글 아이템명까지 읽으려면 ko)
    "target_lang": "ko",       # 번역 결과 언어
    "interval_sec": 0.7,       # 몇 초마다 채팅창을 읽을지
    "deepl_api_key": "",
    "ai_engine": "claude_cli",  # claude_cli(Claude Code) / api(API 키) / off
    "ai_cli_model": "sonnet",   # Claude Code 모델: sonnet(자연스러움, 추천) / haiku(조금 더 빠름)
    "anthropic_api_key": "",    # ai_engine이 api일 때만
    "ai_api_model": "claude-haiku-5-5",       # (선택) DeepL 무료 API 키 - 넣으면 가장 빠르고 정확
    "upscale": 2.5,            # 캡처 확대 배율 (작은 글씨 인식률 향상)
    "max_lines": 25,           # 오버레이에 보여줄 줄 수
    "font_size": 11,
    "show_original": True,     # 번역 아래 원문 표시
    "my_name": "",             # 내 게임 닉네임 (처음 켤 때 물어봄) - 내 채팅은 번역 생략
    "skip_korean_lines": True, # 이미 한국어인 줄은 번역 생략
    "auto_update": True,       # 실행할 때 GitHub에서 새 버전 자동 받기
    "debug": True,             # 문제 확인용 기록(debug_log.txt) 남기기
    "hide_notice": True,       # 게임 공지·가챠 알림 숨기기 (false 면 [전체]에 보임)
    "hide_kinds": [],          # 안 보고 싶은 종류. 예: ["확성기", "고확"]
    "region": None,            # [left, top, width, height] (F9로 지정하면 자동 저장)
    "overlay_geometry": "560x320+40+40",
}


# ----------------------------------------------------------------------------
# 설정 / 사전
# ----------------------------------------------------------------------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:  # noqa: BLE001
            print("config.json 읽기 실패, 기본값 사용:", e)
    if cfg.get("interval_sec") == 1.2:  # 예전 버전 기본값이면 빠른 값으로
        cfg["interval_sec"] = DEFAULT_CONFIG["interval_sec"]
    if cfg.get("max_lines") == 12:  # 예전 기본값이면 더 많이 보이게
        cfg["max_lines"] = DEFAULT_CONFIG["max_lines"]
    if not cfg.get("_mig_haiku_on"):  # 성윤님 요청(10/25): AI 번역 켜고 가벼운 haiku 로 (한 번만 적용)
        cfg["ai_cli_model"] = "haiku"
        cfg["ai_paused"] = False
        cfg["_mig_haiku_on"] = True
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        print("config.json 저장 실패:", e)


KO_TERMS = {}  # 한글 용어 교체 (사전의 ko_terms). 예) 클로 -> 아대


# 최종 표기 다듬기 (성윤님 요청 표기)
FINAL_FIX = [
    (re.compile(r"아대\s*공격력\s*주문서\s*[:：]?\s*(?:중급)?"), "아대공격력 주문서"),
    (re.compile(r"아대\s+공격력"), "아대공격력"),
]


def apply_ko_terms(text):
    for k, v in KO_TERMS.items():  # 낱말 단위로만 (클로버 -> 아대버 방지)
        text = re.sub(r"(?<![가-힣])" + re.escape(k) + r"(?![가-힣])", v, text)
    for pat, rep_ in FINAL_FIX:
        text = pat.sub(rep_, text)
    return re.sub(r"주문서 \]", "주문서]", text)


class Glossary:
    """메이플 용어 사전 처리기."""

    def __init__(self, path=GLOSSARY_PATH):
        self.path = path
        self.reload()

    def reload(self):
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if os.path.exists(MY_GLOSSARY_PATH):
            with open(MY_GLOSSARY_PATH, "r", encoding="utf-8") as f:
                mine = json.load(f)
            for sec in ("expand", "terms", "terms_case", "names", "ko_terms"):
                data.setdefault(sec, {}).update(mine.get(sec) or {})
        clean = lambda d: {k: v for k, v in (d or {}).items() if not k.startswith("_")}
        self.expand = clean(data.get("expand"))
        terms = clean(data.get("terms"))
        terms.update(clean(data.get("names")))
        self.terms = terms
        self.terms_case = clean(data.get("terms_case"))
        self.ko_terms = clean(data.get("ko_terms"))
        KO_TERMS.clear()
        KO_TERMS.update(self.ko_terms)
        self._expand_re = self._build(self.expand, ignore_case=True)
        self._terms_re = self._build(self.terms, ignore_case=True)
        self._terms_case_re = self._build(self.terms_case, ignore_case=False)
        self._lower_expand = {k.lower(): v for k, v in self.expand.items()}
        self._lower_terms = {k.lower(): v for k, v in self.terms.items()}

    @staticmethod
    def _build(mapping, ignore_case):
        if not mapping:
            return None
        parts = []
        # 긴 용어부터 매칭해야 "earring int"가 "earring"보다 먼저 잡힘
        for key in sorted(mapping, key=len, reverse=True):
            esc = re.escape(key).replace(r"\ ", r"\s+")
            left = r"(?<![A-Za-z0-9])" if key[0].isalnum() else ""
            right = r"(?![A-Za-z0-9])" if key[-1].isalnum() else ""
            parts.append(left + esc + right)
        flags = re.IGNORECASE if ignore_case else 0
        return re.compile("|".join(parts), flags)

    # --- 번역 전 처리 ------------------------------------------------------
    def protect(self, text):
        """번역기에 넘기기 전: 금액/용어를 자리표시자로 바꾸고 줄임말을 풀어 씀.
        반환: (번역기에 넘길 문장, 자리표시자 -> 한국어 목록)"""
        slots = []

        def hold(korean):
            slots.append(korean)
            return f" ZZ{len(slots) - 1}QQ "

        text = re.sub(r"\s*[—–]\s*", "-", text)  # 긴 대시 통일 (Ink—and—Wash)
        # 1) 대괄호 아이템 링크: 한글은 그대로, 영어 부분만 번역
        #    [에메랄드 귀고리] -> 그대로 / [Emerald Earrings] -> [에메랄드 귀고리]
        #    [장갑 Attack 주문서] -> 한글 유지 + Attack 만 번역
        def bracket(m):
            inner = m.group(1)
            mm = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(k|kk|m|mil|mill|million)\s*", inner, re.IGNORECASE)
            if mm:  # [700k] -> [70만] (대괄호 안 금액의 k 가 '알았어'로 번역되던 문제)
                num = float(mm.group(1).replace(",", "."))
                return " [ " + hold(format_meso(num * (1_000 if mm.group(2).lower().startswith("k") else 1_000_000))) + " ] "
            if not re.search(r"[A-Za-z]", inner):
                return hold(apply_ko_terms(m.group(0)))  # 한글/숫자만 -> 보존 (클로->아대 같은 교체만)
            out = []
            for part in re.split(r"([A-Za-z][A-Za-z'./ -]*[A-Za-z]|[A-Za-z])", inner):
                if not part:
                    continue
                if re.fullmatch(r"[A-Za-z][A-Za-z'./ -]*", part):
                    key = re.sub(r"\s+", " ", part.strip()).lower()
                    if key in self._lower_terms:
                        out.append(hold(self._lower_terms[key]))
                    elif part.strip() in self.terms_case:
                        out.append(hold(self.terms_case[part.strip()]))
                    else:
                        sub = part
                        if self._terms_re:
                            sub = self._terms_re.sub(lambda t: hold(self._lower_terms.get(
                                re.sub(r"\s+", " ", t.group(0)).lower(), t.group(0))), sub)
                        out.append(sub)  # 남은 영어는 번역기가 번역
                elif part.strip():
                    ko = part.strip()
                    ko = apply_ko_terms(ko)  # 클로 -> 아대 같은 한글 용어 교체
                    out.append(hold(ko))  # 한글·숫자·기호는 그대로
            return " [ " + " ".join(out) + " ] "

        text = re.sub(r"\[([^\]]{1,40})\]", bracket, text)

        # 2) 메소 금액 500k / 1.2m / 2.5mil / 300 mil -> 50만 / 120만
        def money(m):
            num = float(m.group(1).replace(",", ""))
            unit = m.group(2).lower()
            value = num * (1_000 if unit.startswith("k") else 1_000_000)
            return hold(format_meso(value))

        text = re.sub(r"(?<![A-Za-z0-9+.])(\d+(?:[.,]\d+)?)\s*(k|kk|m|mil|mill|million)(?![A-Za-z]|\.\s?(?:def|att|atk|acc|avo|matk|ma\b))",
                      money, text, flags=re.IGNORECASE)

        # 2.5) 한국식 표기로: ch25 / cc18 -> 25채널, x2 / 2x -> 2개, 5str -> 5힘, att69 -> 공69, 3cd -> 크뎀3
        text = re.sub(r"(?<![A-Za-z])(?:CH|Ch|ch)[Il|]\s?(\d)(?![0-9A-Za-z])", lambda m: hold("1" + m.group(1) + "채널"), text)  # OCR: CH17 -> 'CHI 7'
        text = re.sub(r"(?<![A-Za-z])(?:ch|cc|chan)\.?\s?(\d{1,2})(?![0-9A-Za-z])", lambda m: hold(m.group(1) + "채널"), text, flags=re.I)
        text = re.sub(r"(?<![A-Za-z0-9])(\d{1,2})\s?ch(?![A-Za-z])", lambda m: hold(m.group(1) + "채널"), text, flags=re.I)
        text = re.sub(r"(?<![A-Za-z])lvl?\.?\s?(\d{1,3})(?![0-9])", lambda m: hold(m.group(1) + "레벨"), text, flags=re.I)
        text = re.sub(r"(?<![A-Za-z0-9])(\d{1,3})\s?(min|mins|minutes)(?![A-Za-z])", lambda m: hold(m.group(1) + "분"), text, flags=re.I)
        text = re.sub(r"(?<![A-Za-z0-9])(?:\((\d{1,3})\s?left\)|\(?(\d{1,3})\)?\s?left)(?![A-Za-z])",
                      lambda m: hold((m.group(1) or m.group(2)) + "개 남음"), text, flags=re.I)
        text = re.sub(r"(?<![A-Za-z0-9.])(\d{1,3})\s?w?def\s?min(?![A-Za-z])", lambda m: hold("방어력 " + m.group(1) + " 이상"), text, flags=re.I)
        text = re.sub(r"(?<![A-Za-z0-9])(\d{1,3})\s?(sets?)(?![A-Za-z])", lambda m: hold(m.group(1) + "세트"), text, flags=re.I)
        text = re.sub(r"(?<![A-Za-z0-9])x\s?(\d{1,3})(?![0-9A-Za-z%])|(?<![A-Za-z0-9])(\d{1,3})\s?x(?![A-Za-z])",
                      lambda m: hold((m.group(1) or m.group(2)) + "개"), text, flags=re.I)
        _stat = {"str": "힘", "dex": "민첩", "int": "지력", "luk": "운", "att": "공", "atk": "공", "watk": "공", "ma": "마", "cr": "크리",
                 "matk": "마", "matt": "마", "watt": "공", "acc": "명중", "crit": "크리", "cd": "크뎀", "cdmg": "크뎀", "wa": "공"}
        text = re.sub(r"(?<![A-Za-z0-9.])(\d{1,3})\s?([mw]?at)[l1I]<", lambda m: m.group(1) + " " + m.group(2) + "k", text, flags=re.I)  # OCR: 'Matl<' = Matk
        text = re.sub(r"(?<![A-Za-z0-9.])(\d{1,3})\s?M\.\s?(atk|att)(?![A-Za-z])", lambda m: hold(m.group(1) + "마"), text, flags=re.I)  # 101M.Atk -> 101마
        text = re.sub(r"(?<![A-Za-z0-9.])(\d{1,3})\s?(str|dex|int|luk|watk|watt|matk|matt|atk|att|acc|crit|cdmg|cd|cr|wa|ma)(?![A-Za-z])",
                      lambda m: hold(m.group(1) + _stat[m.group(2).lower()]), text, flags=re.I)
        text = re.sub(r"(?<![A-Za-z])(watk|matk|matt|att|ma)(\d{2,3})(?![0-9A-Za-z%])",
                      lambda m: hold(_stat[m.group(1).lower()] + m.group(2)), text, flags=re.I)

        # 3) +2 / 60% 같은 숫자는 그대로 두되 주문서 표기는 보존
        text = re.sub(r"(?<!\d)(10|30|60|70|100)\s*%", lambda m: hold(m.group(1) + "%"), text)

        # 4) 일반 메이플 용어 (S>, c/o, a/w, clean, kpq ...)
        if self._terms_re:
            def term(m):
                key = re.sub(r"\s+", " ", m.group(0)).lower()
                return hold(self._lower_terms.get(key, m.group(0)))
            text = self._terms_re.sub(term, text)

        # 5) 대소문자 구분 용어 (MA, INT, KS ...)
        if self._terms_case_re:
            text = self._terms_case_re.sub(lambda m: hold(self.terms_case[m.group(0)]), text)

        # 6) 채팅 줄임말 풀어쓰기 (gz -> congratulations ...)
        if self._expand_re:
            text = self._expand_re.sub(
                lambda m: self._lower_expand.get(m.group(0).lower(), m.group(0)), text)

        # 7) 반복 문자 정리 (LEBLANCCCCCCC, ?????)
        text = re.sub(r"([A-Za-z])\1{3,}", r"\1", text)
        text = re.sub(r"([!?.])\1{2,}", r"\1", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
        return text, slots

    @staticmethod
    def restore(translated, slots):
        def back(m):
            idx = int(m.group(1))
            return slots[idx] if idx < len(slots) else m.group(0)

        out = re.sub(r"Z\s*Z\s*(\d+)\s*Q\s*Q", back, translated, flags=re.IGNORECASE)
        # 번역기가 자리표시자를 통째로 날린 경우 대비: 빠진 용어는 뒤에 붙여줌
        missing = [s for i, s in enumerate(slots) if s not in out]
        if missing:
            out = out + "  (" + ", ".join(missing) + ")"
        out = apply_ko_terms(re.sub(r"\s+([,.!?:])", r"\1", out))
        out = re.sub(r"\+\s+(\d)", r"+\1", out)
        out = re.sub(r"(\d)-?\s*칸 확장 쿠폰", r"\1칸 확장 쿠폰", out)  # [Selective 5-slot Coupon] -> 선택 5칸 확장 쿠폰
        out = re.sub(r"(표창|주문서|귀고리|장갑|완드|스태프|상자|쿠폰)(\s+\1)+", r"\1", out)  # '월비 표창 표창' 같은 중복 정리
        out = re.sub(r"(\[(팝니다|삽니다|교환합니다|구합니다)\])(\s*,?\s*\1)+", r"\1", out)  # [팝니다] [팝니다] -> 하나로
        out = re.sub(r"\[\s+", "[", out)
        out = re.sub(r"\s+\]", "]", out)
        return re.sub(r"\s{2,}", " ", out).strip()


def format_meso(value):
    value = int(round(value))
    if value >= 100_000_000:
        eok, rest = divmod(value, 100_000_000)
        man = rest // 10_000
        return f"{eok}억{man}만" if man else f"{eok}억"
    if value % 100 and value >= 10_000:  # 64.99k -> 6만4990 (십 단위까지 살림, 9백으로 깎이던 문제)
        return f"{value // 10_000}만{value % 10_000:04d}"
    if value % 100 and value >= 1_000:  # 1.55k -> 1550메소
        return f"{value}메소"
    if value >= 10_000:  # 한국식: 17만5천 / 1만8천 / 120만
        man, rest = divmod(value, 10_000)
        chun, rest = divmod(rest, 1_000)
        baek = rest // 100
        if man < 100 and baek:  # 17.5k -> 1만7천5백 (백 단위를 버리면 가격이 틀어짐)
            return f"{man}만{chun}천{baek}백" if chun else f"{man}만{baek}백"
        return f"{man}만{chun}천" if chun else f"{man}만"
    if value >= 1_000:
        chun, rest = divmod(value, 1_000)
        return f"{chun}천{rest // 100}백" if rest >= 100 else f"{chun}천"
    return f"{value:,}메소"


# ----------------------------------------------------------------------------
# 번역
# ----------------------------------------------------------------------------
class Translator:
    """무료 번역 엔진 여러 개를 돌려 쓰는 번역기.
    한 엔진이 '요청이 너무 많다'고 막으면 기다리지 않고 바로 다음 엔진으로 넘어감.
    (DeepL 무료 API 키를 config.json의 deepl_api_key 에 넣으면 그걸 1순위로 사용)"""

    def __init__(self, target="ko", deepl_key=""):
        self.target = target
        self.cache = {}
        self.deepl_key = (deepl_key or "").strip()
        import requests
        self._session = requests.Session()
        self._session.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
        self.engines = []
        if self.deepl_key:
            self.engines.append(("DeepL", self._deepl))
        self.engines += [("Google", self._google_gtx), ("Google2", self._google_dict),
                         ("MyMemory", self._mymemory)]
        self.blocked = {}  # 엔진 이름 -> 다시 써도 되는 시각

    @staticmethod
    def needs_translation(text):
        return bool(re.search(r"[A-Za-zÀ-ÿ]{2,}", re.sub(r"ZZ\d+QQ", "", text)))

    def _get(self, url, **params):
        r = self._session.get(url, params=params, timeout=6)
        if r.status_code == 429:
            raise RuntimeError("TooManyRequests")
        r.raise_for_status()
        return r.json()

    def _google_gtx(self, text):
        data = self._get("https://translate.googleapis.com/translate_a/single",
                         client="gtx", sl="auto", tl=self.target, dt="t", q=text)
        return "".join(seg[0] for seg in data[0] if seg and seg[0])

    def _google_dict(self, text):
        data = self._get("https://clients5.google.com/translate_a/t",
                         client="dict-chrome-ex", sl="auto", tl=self.target, q=text)
        first = data[0] if isinstance(data, list) else data
        if isinstance(first, list):
            first = first[0]
        if not isinstance(first, str):
            raise ValueError("unexpected response")
        return first

    def _mymemory(self, text):
        data = self._get("https://api.mymemory.translated.net/get",
                         q=text, langpair=f"en|{self.target}")
        out = (data.get("responseData") or {}).get("translatedText") or ""
        if not out or "MYMEMORY WARNING" in out.upper():
            raise RuntimeError("TooManyRequests")
        return out

    def _deepl(self, text):
        host = "api-free.deepl.com" if self.deepl_key.endswith(":fx") else "api.deepl.com"
        r = self._session.post(f"https://{host}/v2/translate", timeout=6,
                               headers={"Authorization": f"DeepL-Auth-Key {self.deepl_key}"},
                               data={"text": text, "target_lang": self.target.upper()})
        if r.status_code in (429, 456):
            raise RuntimeError("TooManyRequests")
        r.raise_for_status()
        return r.json()["translations"][0]["text"]

    def _request(self, text):
        last_err = "번역 엔진이 모두 쉬는 중"
        # 너무 자주 보내면 구글이 막음 -> 요청 사이 최소 1.2초
        gap = time.time() - getattr(self, "_last_req", 0)
        if gap < 1.2:
            time.sleep(1.2 - gap)
        self._last_req = time.time()
        now = time.time()
        for name, fn in self.engines:
            if self.blocked.get(name, 0) > now:
                continue
            try:
                return fn(text)
            except Exception as e:  # noqa: BLE001
                last_err = f"{name}: {type(e).__name__} {str(e)[:80]}"
                dlog("ENGINE-ERR", last_err)
                # 막힌 엔진은 잠시 쉬게 하고 바로 다음 엔진 시도
                self.blocked[name] = now + (30 if "TooMany" in str(e) else 5)
        raise RuntimeError(last_err)

    def translate_many(self, texts):
        """texts 목록을 번역해서 같은 순서로 돌려줌. 실패하면 None 자리에 원문."""
        results = list(texts)
        self.failed = set()  # 번역 실패해서 원문 그대로 둔 줄 번호 (나중에 다시 시도)
        todo = [i for i, t in enumerate(texts) if self.needs_translation(t)]
        for i in list(todo):
            if texts[i] in self.cache:
                results[i] = self.cache[texts[i]]
                todo.remove(i)
        if not todo:
            return results, None
        joined = "\n".join(texts[i] for i in todo)
        try:
            out = self._request(joined)
        except Exception as e:  # noqa: BLE001
            self.failed = set(todo)
            dlog("TR-ERR", repr(e))
            return results, f"번역 엔진이 모두 잠깐 막혔어요 ({e}). 곧 자동으로 다시 돼요"
        parts = out.split("\n")
        if len(parts) != len(todo):
            # 줄 수가 안 맞으면 한 줄씩 다시 (드문 경우)
            parts = []
            dlog("TR-SPLIT", len(parts), len(todo))
            for i in todo:
                try:
                    parts.append(self._request(texts[i]))
                except Exception as e:  # noqa: BLE001
                    dlog("TR-ERR", repr(e))
                    parts.append(None)
        for i, tr in zip(todo, parts):
            if not tr or not tr.strip():
                self.failed.add(i)  # 실패한 줄은 캐시에 넣지 않음 -> 다음에 다시 번역
                continue
            results[i] = tr.strip()
            self.cache[texts[i]] = tr.strip()
        if len(self.cache) > 3000:
            self.cache.clear()
        return results, None

    def translate(self, text):
        res, err = self.translate_many([text])
        return res[0] + (f"  ({err})" if err else "")


# ----------------------------------------------------------------------------
# 채팅 줄 파싱 / 중복 제거
# ----------------------------------------------------------------------------
CHANNEL_RE = re.compile(r"(?<![A-Za-z])[CcGg][Hh]\s*[0-9OoZzSsIlB]{1,2}(?![A-Za-z]{2})")
LINE_RE = re.compile(r"^\s*([^:：;]{1,32}?)\s*[:：;]\s*(.+)$")
HANGUL_RE = re.compile(r"[가-힣]")


BADGE_RE = re.compile(r"\s*(?<![A-Za-z])[CcGg][Hh]\s*([0-9OoZzSsIlB]{1,2})(?![A-Za-z]{2})\s*[^A-Za-z0-9가-힣\[\s]{0,3}\s*")


def _clean_name(part):
    """닉네임은 번역하지 않고 인식된 그대로 사용 (앞뒤 기호만 정리)."""
    raw = CHANNEL_RE.sub("", part).strip(" \t:;：@®©*·•|'\"`\\/.,()[]{}<>~")
    raw = re.sub(r"\s+[0-9OoIl|\]\[)(]{1,2}$", "", raw)  # 'Gutta 0]' 처럼 배지 찌꺼기가 붙은 경우
    if len(re.sub(r"[^A-Za-z0-9가-힣]", "", raw)) < 3:
        return None  # 'm', 'ae' 처럼 닉네임 일부만 읽힌 것
    return raw or None


_CH_FIX = str.maketrans({"O": "0", "o": "0", "Z": "2", "z": "2", "S": "5", "s": "5",
                         "I": "1", "l": "1", "B": "8"})


# 문장 첫 단어라서 닉네임으로 착각하면 안 되는 말
_NOT_NAMES = set("well sure yeah okay also then what maybe lmao haha thanks sorry nope true same wait damn guys "
                 "yes nice cool lets dude bruh look also just here there this that they wtb wts sell selling buying".split())


def parse_line(raw):
    """채팅 한 줄을 (닉네임, 대화내용, 채널)로 분리.
    형식 1) 닉네임 CH12 ♥ : 대화   (확성기 - CH 배지 왼쪽이 닉네임, 배지가 채널)
    형식 2) 닉네임 : 대화           (일반/파티)
    배지는 있는데 대화가 없으면 ('닉네임', '', 채널) -> 다음 줄에 대화가 이어짐."""
    line = raw.strip()
    w = re.match(r"^\s*([A-Za-z0-9가-힣_]{3,13})\s*(>>|<<)\s*(.+)$", line)
    if w:  # 귓속말: 'Name>> 내용' (받은 귓) / 'Name<< 내용' (보낸 귓)
        return w.group(1), w.group(3).strip(), ("받은 귓" if w.group(2) == ">>" else "보낸 귓")
    if "\x01" in line:  # 배지 위치로 이미 나눠진 줄
        name, ch, msg = (line.split("\x01") + ["", ""])[:3]
        return (_clean_name(name) or None), msg.strip(), (ch or None)
    line = re.sub(r"(?:(?<=\s)|^)[Il|!1\[(]([CcGg][Hh]\s*[0-9OoZzSsIlB]{1,2})", r"\1", line)  # 배지 테두리 찌꺼기
    badge = BADGE_RE.search(line)
    if badge and badge.start() <= 26:
        name = _clean_name(line[:badge.start()])
        ch = "CH" + badge.group(1).translate(_CH_FIX)
        rest = line[badge.end():]
        rest = re.sub(r"^\s*[:：;]\s*", "", rest).strip()  # 배지 뒤 콜론 제거 (OCR이 놓쳐도 OK)
        if name:
            return name, rest, ch
        if badge.start() <= 2:  # 닉네임 부분을 못 읽었어도 배지는 떼고 채널만 기억
            return None, rest, ch
    m = LINE_RE.match(line)
    if not m:
        # 일반 채팅 'cream : 내용'의 콜론을 OCR이 . • « ' 등으로 읽은 경우
        m3 = re.match(r"^\s*[•·*]?\s*([A-Za-z0-9_]{4,13})\s*[.•·«»'`]{1,2}\s+(\S.*)$", line)
        if m3 and _clean_name(m3.group(1)) and m3.group(1).lower() not in _NOT_NAMES:
            return _clean_name(m3.group(1)), m3.group(2).strip(), None
        m2 = re.match(r"^\s*([^:：;\s][^:：;]{0,30}?)\s*[:：;]\s*$", line)
        if m2 and _clean_name(m2.group(1)) and len(m2.group(1).split()) <= 2:
            return _clean_name(m2.group(1)), "", None  # 닉네임만 있고 대화는 다음 줄
        return None, line, None
    name = _clean_name(m.group(1))
    msg = m.group(2).strip()
    if name and (len(name.split()) > 2 or re.search(r"[!?,.]", name)):
        return None, line, None  # "ate! Congrats! : ..." 같은 건 닉네임이 아님
    return (name, msg, None) if name else (None, msg, None)


def normalize(s):
    s = re.sub(r"\x01[^\x01]*\x01", " ", s)  # 채널 배지 표시는 비교에서 뺌
    return re.sub(r"[^a-z0-9가-힣]", "", s.lower())


_WORD_OK = re.compile(r"^[\[(\"'*]*((?=[A-Za-z']*[aeiouyAEIOUY])(?:[A-Z]?[a-z']+|[A-Z']+)|\d+([.,]\d+)?(?i:k|m|kk|%|x)?|[SBTLJ]>|(?i:ch|lv)\.?\d+|\d+(?i:[a-z]{1,4})|[A-Z]{2,5}|(?i:ok|k|u|ty|gz|lol|pm|pmo|pm0|nx|fm|cc|ea|obo|a/w|c/o|sry|brb|omw|rn|tyty|thx|gj|gl|np|nvm|btw|wtf|smh|ngl|tbh)|[:/&+\-<>]|>>|<<)[\])\"'.,!?:;~]*$")


_CHAT_SHORT = {"sry", "ok", "ty", "lol", "brb", "omw", "gz", "pm", "hi", "hey", "yo", "np", "gg", "plz", "pls"}


def is_my_nametag(msg, my_name):
    """닉네임 없이 읽힌 줄이 캐릭터 머리 위 내 이름표(SungWoong + 펫·길드 이름)뿐인지.
    채팅 영역이 게임 화면에 겹치면 이름표를 채팅으로 잘못 읽어 번역 엔진을 낭비함.
    내 이름 말고 대화 같은 단어(소문자 단어·sry·가격 등)가 하나라도 있으면 채팅으로 보고 남김."""
    me = re.sub(r"[^a-z]", "", (my_name or "").lower())
    if len(me) < 5:
        return False
    frags = [me[i:i + 5] for i in range(len(me) - 4)]
    found = False
    for tok in (msg or "").split():
        flat = re.sub(r"[^a-z]", "", tok.lower().replace("vv", "w").replace("0", "o"))
        if any(f in flat for f in frags):
            found = True
            continue  # 내 이름(이 섞인) 토큰
        word = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", tok)
        if re.fullmatch(r"[a-z]{2,}", word) or word.lower() in _CHAT_SHORT or re.search(r"\d\s*[kKmM]\b", word):
            return False  # 대화 단어가 있음 -> 진짜 채팅일 수 있음
    return found



_NAMETAG_STOP = {"congrats", "grats", "thanks", "nice", "how", "what", "the", "you", "and", "for", "sell", "selling",
                 "buy", "buying", "price", "offer", "party", "guild", "fashion", "crate", "mystery", "aurora", "obtained",
                 "ngl", "tbh", "lmao", "lol", "omg", "thx", "plz", "pls", "gg", "wait", "here", "where", "lets", "come",
                 "want", "need", "have", "just", "this", "that", "with", "yes", "yeah", "nope", "okay", "sorry", "thank"}
NAMETAG_WORDS = set()  # 내 이름표 옆에 같이 읽히는 단어 (펫 이름·길드 이름 등) - 실행 중에 배움


def nametag_like(msg, my_name):
    """닉네임 없는 짧은 줄이 캐릭터 이름표 조각인지 (채팅창이 투명해서 뒤에 있는 캐릭터 이름이 읽힌 것).
    'song oong', 'Blac UUI', 'Black', 'SungWoong31ack Kitty' 같은 것."""
    me = re.sub(r"[^a-z]", "", (my_name or "").lower())
    toks = [t for t in re.split(r"\s+", msg or "") if re.search(r"[A-Za-z]", t)]  # 글자 없는 기호 조각은 빼고 셈
    if not toks or len(toks) > 5:
        return False
    letters = re.sub(r"[^a-z]", "", (msg or "").lower().replace("vv", "w").replace("0", "o"))
    if not letters:
        return False
    if len(me) >= 5 and len(letters) <= len(me) + 10:  # 긴 줄(파티 대화 등)은 우연히 글자가 겹칠 수 있음
        # 내 이름과 글자가 많이 겹치면 이름표 (일부 글자가 깨져도)
        sm = SequenceMatcher(None, me, letters)
        common = sum(b.size for b in sm.get_matching_blocks())
        if common >= max(4, int(len(me) * 0.6)):
            for t in toks:  # 같이 붙어 읽힌 다른 단어(펫·길드 이름)도 기억
                w = re.sub(r"[^A-Za-z]", "", t)
                if (len(w) >= 3 and not w.isupper() and w.lower() not in _NAMETAG_STOP and w.lower() not in _CHAT_SHORT
                        and SequenceMatcher(None, me, w.lower()).ratio() < 0.5 and len(NAMETAG_WORDS) < 60):
                    NAMETAG_WORDS.add(w.lower())  # 펫·길드 이름은 대문자로 시작 (Black Kitty)
            return True
    def known(w):
        for k in NAMETAG_WORDS:
            if w == k or (len(w) >= 3 and (k.startswith(w) or w.startswith(k[:4]))):
                return True
            if len(w) >= 3 and len(k) >= 4 and (w in k or k in w):  # 'itty' / 'Kitty', 'lack' / 'Black'
                return True
            if len(w) >= 4 and SequenceMatcher(None, w, k).ratio() >= 0.8:  # 'Kittv' / 'Kitty'
                return True
        return False

    def mine(w):  # 내 이름 조각 ('sung', 'oong', 'sungv', 'sunc')
        if len(me) < 5 or len(w) < 3:
            return False
        if w in me or any(me[i:i + 4] in w for i in range(len(me) - 3)):
            return True
        n = len(w)
        return n <= len(me) and max(SequenceMatcher(None, w, me[i:i + n]).ratio() for i in range(len(me) - n + 1)) >= 0.75

    hit = rest_chat = False
    unknown = []
    for t in toks:
        w = re.sub(r"[^a-z]", "", t.lower().replace("vv", "w").replace("0", "o"))
        if not w:
            continue
        if known(w) or mine(w):
            hit = True
            continue
        word = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", t)
        if re.fullmatch(r"[a-z]{2,}", word) or word.lower() in _CHAT_SHORT or len(word) > 4:
            rest_chat = True  # 대화 같은 단어가 섞여 있으면 진짜 채팅일 수 있음
            unknown.append(word)
    if hit and not rest_chat:
        return True
    # 'Black Kitty Sung' 처럼 이름표 조각이 2개 이상이고 모르는 단어가 1개뿐이면 그 단어도 이름표(펫·길드 이름)로 배움
    if hit and len(unknown) == 1 and len(toks) - 1 >= 2 and len(NAMETAG_WORDS) < 60 \
            and unknown[0].lower() not in _CHAT_SHORT and unknown[0].lower() not in _NAMETAG_STOP \
            and unknown[0][:1].isupper() and not unknown[0].isupper():
        NAMETAG_WORDS.add(re.sub(r"[^a-z]", "", unknown[0].lower()))
        return True
    return False

def text_quality(text):
    """OCR 결과가 얼마나 '사람이 쓴 글' 같은지 0~1 점수 (찌꺼기 걸러내기·두 OCR 결과 중 고르기용)."""
    t = re.sub(r"\x01[^\x01]*\x01", " ", text or "")
    t = re.sub(r"\[[^\]]*\]", " ", t)  # 아이템 이름은 빼고 평가
    t = re.sub(r"[•·]", " ", t)  # 줄 앞 점(•)은 평가에서 뺌
    t = re.sub(r",(?=[A-Za-z])", ", ", t)  # "Yes,full" -> "Yes, full"
    toks = t.split()
    if not toks:
        return 0.0
    good = sum(1 for w in toks if _WORD_OK.match(w))
    junk = len(re.findall(r"[^A-Za-z0-9가-힣\s.,!?:;'\"/%+\-<>()\[\]&#@*~=_]", t))
    return max(0.0, good / len(toks) - 0.15 * junk / max(1, len(t)) * 10)


def _same_line(a, b):
    """같은 채팅 줄인지 (OCR이 앞부분 닉네임을 읽었다/못 읽었다 해도 같은 줄로 봄)."""
    if a == b:
        return True
    if re.findall(r"\d+", a) != re.findall(r"\d+", b):
        # 숫자(가격·%)가 다르면 다른 글 (175k vs 150k, 60% vs 30%) - 아주 비슷할 때만 같은 줄
        return SequenceMatcher(None, a, b).ratio() > 0.96 and abs(len(a) - len(b)) <= 1
    sm = SequenceMatcher(None, a, b)
    if sm.ratio() > 0.88:
        return True
    short = min(len(a), len(b))
    if short >= 12:
        matched = sum(m.size for m in sm.get_matching_blocks())
        return matched >= short * 0.92
    return False


class AIMemory:
    """AI(Claude)가 번역한 결과를 파일(ai_memory.json)에 쌓아두고,
    같은 채팅(확성기 반복 광고 등)이 다시 오면 Claude 없이 바로 재사용."""

    def __init__(self, path=AI_MEMORY_PATH, size=8000):
        self.path, self.size = path, size
        self.data = {}
        self.dirty = False
        self.lock = threading.Lock()
        self.hits = 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:  # noqa: BLE001
            self.data = {}
        # 오번역 일괄 수정 파일(ai_memory_fix.json)이 있으면 반영하고 .applied 로 이름 바꿈
        fix_path = os.path.join(os.path.dirname(path) or ".", "ai_memory_fix.json")
        if os.path.exists(fix_path):
            try:
                with open(fix_path, "r", encoding="utf-8") as f:
                    fix = json.load(f)
                for k in fix.get("delete", []):
                    self.data.pop(k, None)
                for k, v in (fix.get("set") or {}).items():
                    old = self.data.get(k, {})
                    self.data[k] = {"orig": v.get("orig", old.get("orig", "")), "tr": v["tr"],
                                    "n": old.get("n", 1), "t": old.get("t", int(time.time()))}
                self.dirty = True
                self.save()
                os.replace(fix_path, fix_path + ".applied")
                dlog("MEMORY-FIX", len(fix.get("set") or {}), "set", len(fix.get("delete", [])), "deleted")
            except Exception as e:  # noqa: BLE001
                dlog("MEMORY-FIX-ERR", repr(e))

    @staticmethod
    def key(text):
        t = (text or "").lower()
        t = re.sub(r"(?<=\d)[ol]|[ol](?=\d)", lambda m: "0" if m.group(0) == "o" else "1", t)  # 6O% -> 60%
        t = re.sub(r"pm0\b", "pmo", t)
        t = re.sub(r"[^a-z0-9가-힣%+>/]", "", t)
        return t[:200]

    def get(self, orig):
        k = self.key(orig)
        if len(k) < 4:
            return None
        with self.lock:
            hit = self.data.get(k)
            if not hit:
                # OCR이 글자를 조금 다르게 읽은 경우: 숫자(가격)가 똑같을 때만 비슷한 문장 재사용
                digits = re.findall(r"\d+", k)
                recent = sorted(self.data.items(), key=lambda kv: kv[1].get("t", 0), reverse=True)[:600]
                for ok, ov in recent:
                    if abs(len(ok) - len(k)) <= 4 and re.findall(r"\d+", ok) == digits \
                            and SequenceMatcher(None, ok, k).ratio() > 0.93:
                        hit = ov
                        break
            if hit:
                hit["n"] = hit.get("n", 1) + 1
                hit["t"] = int(time.time())
                self.hits += 1
                self.dirty = True
                return hit["tr"]
        return None

    def add(self, orig, tr):
        k = self.key(orig)
        if len(k) < 4 or not tr:
            return
        with self.lock:
            old = self.data.get(k, {})
            self.data[k] = {"orig": orig, "tr": tr, "n": old.get("n", 1), "t": int(time.time())}
            if len(self.data) > self.size:
                for dk, _v in sorted(self.data.items(), key=lambda kv: kv[1].get("t", 0))[:len(self.data) - self.size]:
                    del self.data[dk]
            self.dirty = True

    def save(self):
        if not self.dirty:
            return
        with self.lock:
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=0)
                os.replace(tmp, self.path)
                self.dirty = False
            except Exception as e:  # noqa: BLE001
                dlog("MEMORY-SAVE-ERR", repr(e))


class SeenLines:
    def __init__(self, size=300):
        self.items = []
        self.size = size

    def seen(self, line):
        """이미 본 줄인지만 확인 (기록은 안 함)."""
        key = normalize(line)
        if len(key) < 3:
            return True
        for old in reversed(self.items[-80:]):
            if _same_line(old, key):
                return True
        return False

    def match_key(self, line):
        """이미 본 줄이면 그때 기록한 키를 돌려줌."""
        key = normalize(line)
        if len(key) < 3:
            return None
        for old in reversed(self.items[-80:]):
            if _same_line(old, key):
                return old
        return None

    def new_lines(self, lines):
        """채팅은 항상 아래에 새로 붙으니까, 화면에서 '이미 본 마지막 줄' 아래만 새 줄로 인정.
        (위쪽 줄을 OCR이 조금 다르게 읽어서 옛날 채팅이 뒤늦게 다시 뜨는 순서 꼬임 방지)"""
        flags = [self.seen(raw) for raw in lines]
        # 너무 짧은 줄(찌꺼기)은 '이미 본 줄' 기준점으로 쓰지 않음
        last = max((i for i, f in enumerate(flags) if f and len(normalize(lines[i])) >= 6), default=-1)
        out = []
        for i, raw in enumerate(lines):
            if not flags[i]:
                self.is_new(raw)  # 기록
                if i > last:
                    out.append(i)
        return out

    def is_new(self, line):
        key = normalize(line)
        if len(key) < 3:
            return False
        for old in reversed(self.items[-80:]):
            if _same_line(old, key):
                return False
        self.items.append(key)
        if len(self.items) > self.size:
            self.items = self.items[-self.size:]
        return True


# ----------------------------------------------------------------------------
# 화면 캡처 + 윈도우 OCR
# ----------------------------------------------------------------------------
class ScreenOCR:
    def __init__(self, lang="en", upscale=2.5):
        self.lang = lang
        self.upscale = upscale
        import mss  # noqa: F401
        import winocr  # noqa: F401
        self._mss = None
        self._ko = None

    def grab(self, region):
        import mss
        from PIL import Image
        left, top, width, height = region
        # mss 는 만든 스레드에서만 동작함 (끄고 다시 켜면 스레드가 바뀌어 BitBlt 오류) -> 스레드마다 새로 만듦
        tid = threading.get_ident()
        img = None
        for _ in range(2):
            try:
                if self._mss is None or getattr(self, "_mss_tid", None) != tid:
                    self._mss = mss.mss()
                    self._mss_tid = tid
                shot = self._mss.grab({"left": left, "top": top, "width": width, "height": height})
                img = Image.frombytes("RGB", shot.size, shot.rgb)
                break
            except Exception as e:  # noqa: BLE001
                dlog("GRAB-ERR", repr(e))
                self._mss = None
        if img is None:  # 그래도 안 되면 Pillow 캡처로 대신
            from PIL import ImageGrab
            img = ImageGrab.grab(bbox=(left, top, left + width, top + height), all_screens=True).convert("RGB")
        self._orig = img
        if self.upscale and self.upscale != 1:
            img = img.resize((int(img.width * self.upscale), int(img.height * self.upscale)),
                             Image.LANCZOS)
        return img

    def read_exp(self, region):
        """경험치 영역을 읽어서 (글자, parse_exp 결과). 그냥 읽기 -> 안 되면 흰 글자만 남겨서 다시."""
        import asyncio
        import winocr
        from PIL import ImageOps
        img = self.grab(region)

        def rec(im):
            lang = "ko" if getattr(self, "_no_en", False) else "en"
            try:
                res = asyncio.run(winocr.to_coroutine(winocr.recognize_pil(im, lang)))
            except AssertionError:
                self._no_en = True
                res = asyncio.run(winocr.to_coroutine(winocr.recognize_pil(im, "ko")))
            return " ".join((getattr(ln, "text", "") or "") for ln in (getattr(res, "lines", None) or []))

        tries = []
        txt = rec(img)
        tries.append(txt)
        rd = parse_exp(txt)
        if not rd or rd["pct"] is None:
            g = img.convert("L")
            for th in (170, 120):
                bw = g.point(lambda v, th=th: 0 if v >= th else 255).convert("RGB")
                t2 = rec(ImageOps.expand(bw, border=12, fill="white"))
                tries.append(t2)
                r2 = parse_exp(t2)
                if r2 and (not rd or (r2["pct"] is not None and rd["pct"] is None)):
                    rd, txt = r2, t2
                if rd and rd["pct"] is not None:
                    break
        return " / ".join(tries), rd

    def _ocr(self, img, lang):
        """OCR 후 단어들을 '같은 높이'끼리 다시 묶어서 한 줄로 만든다.
        (CH 배지·하트 아이콘 때문에 윈도우 OCR이 한 줄을 여러 조각으로 쪼개는 문제 해결)"""
        import asyncio
        import winocr
        if lang == "en" and getattr(self, "_no_en", False):
            lang = "ko"  # 영어 OCR 언어팩이 없는 PC -> 한국어 OCR로 대신 읽기 (영어도 읽힘)
        try:
            result = asyncio.run(winocr.to_coroutine(winocr.recognize_pil(img, lang)))
        except AssertionError:
            if lang == "en":
                self._no_en = True
                OCR_NOTICE[0] = ("영어 OCR 언어팩이 없어서 한국어 OCR로 대신 읽고 있어요. 더 정확하게 하려면 설치하기.bat 를 "
                                 "다시 실행하거나 [설정 > 시간 및 언어 > 언어]에서 English (United States) 를 추가하세요.")
                result = asyncio.run(winocr.to_coroutine(winocr.recognize_pil(img, "ko")))
            else:
                raise
        scale = self.upscale or 1
        words = []
        for ln in getattr(result, "lines", None) or []:
            try:
                for w in ln.words:
                    r = w.bounding_rect
                    t = (getattr(w, "text", "") or "").strip()
                    if t:
                        words.append((t, r.x / scale, r.y / scale, r.width / scale, r.height / scale))
            except Exception:  # noqa: BLE001
                t = (getattr(ln, "text", "") or "").strip()
                if t:
                    words.append((t, 0, len(words) * 20, 10, 12))
        if not words:
            return []
        words.sort(key=lambda w: w[2] + w[4] / 2)
        rows = []  # [cy, h, [words]]
        for w in words:
            cy = w[2] + w[4] / 2
            if rows and abs(cy - rows[-1][0]) <= max(rows[-1][1], w[4]) * 0.5:
                row = rows[-1]
                row[2].append(w)
                row[0] = sum(x[2] + x[4] / 2 for x in row[2]) / len(row[2])
                row[1] = max(row[1], w[4])
            else:
                rows.append([cy, w[4], [w]])
        lines = []
        for _cy, _h, ws in rows:
            ws.sort(key=lambda w: w[1])
            text = " ".join(w[0] for w in ws)
            x0 = min(w[1] for w in ws); y0 = min(w[2] for w in ws)
            x1 = max(w[1] + w[3] for w in ws); y1 = max(w[2] + w[4] for w in ws)
            lines.append((text, (int(x0), int(y0), int(x1 - x0), int(y1 - y0)), ws))
        return lines

    # --- CH 배지(빨간 글씨 알약 모양) 위치를 색으로 찾아서 닉네임/대화를 정확히 나눔 ---
    @staticmethod
    def _is_badge_red(p):
        return p[0] > 140 and p[1] < 45 and p[2] < 45

    def _find_badge(self, rect):
        """줄 안에서 빨간 CH 글씨 덩어리의 x 범위 (원본 좌표). 없으면 None."""
        img = self._orig
        px = img.load()
        x, y, w, h = rect
        y0, y1 = max(0, y), min(img.height, y + h)
        x0, x1 = 0, img.width  # 줄 전체 폭 (OCR이 배지를 아예 못 읽었어도 찾을 수 있게)
        cols = []
        for cx in range(x0, x1):
            if any(self._is_badge_red(px[cx, cy]) for cy in range(y0, y1)):
                cols.append(cx)
        if not cols:
            return None
        groups, start, prev = [], cols[0], cols[0]
        for c in cols[1:]:
            if c - prev > 3:
                groups.append((start, prev))
                start = c
            prev = c
        groups.append((start, prev))
        for g0, g1 in groups:
            width = g1 - g0 + 1
            if 6 <= width <= max(45, h * 3):
                # 진짜 CH 배지는 흰 알약 위의 빨간 글씨 -> 주변에 흰색이 충분히 있어야 함
                # (채팅창이 투명해서 뒤에 비친 맵의 빨간 부분을 배지로 착각하는 것 방지)
                tot = white = 0
                for cx in range(max(0, g0 - 2), min(img.width, g1 + 3)):
                    for cy in range(y0, y1):
                        p = px[cx, cy]
                        tot += 1
                        if min(p) > 200:
                            white += 1
                if tot and white < tot * 0.15:
                    continue
                # 배지 = 빨간 글씨 앞 여백 + 글씨 + 귀 아이콘
                return g0 - 3, g1 + int(width * 0.9) + 3, (g0, g1)
        return None

    def _apply_badges(self, img, lines):
        """배지가 있는 줄을 '닉네임 \x01채널\x01 대화' 형태로 다시 조립."""
        out, badges = [], []
        retry_left = [4]  # 한 번에 최대 4줄까지만 다시 읽기 (속도)
        for text, rect, ws in lines:
            found = None
            try:
                found = self._find_badge(rect) if ws else None
            except Exception:  # noqa: BLE001
                found = None
            if not found:
                out.append((text, rect, ws))
                continue
            b0, b1, red = found
            name_parts, msg_parts = [], []
            for t, wx, _wy, ww, _wh in ws:
                if wx + ww <= b0 + 2:
                    name_parts.append(t)
                elif wx >= b1 - 2:
                    msg_parts.append(t)
                elif wx < b0:  # 닉네임과 배지가 한 단어로 붙어서 읽힌 경우: 글자 비율로 자르기
                    keep = int(len(t) * (b0 - wx) / max(1, ww))
                    if keep > 0:
                        name_parts.append(t[:keep])
                elif wx + ww > b1:
                    cut = int(len(t) * (b1 - wx) / max(1, ww))
                    if t[cut:].strip():
                        msg_parts.append(t[cut:])
            if not name_parts and b0 > 12 and retry_left[0] > 0:
                retry_left[0] -= 1
                guess = self._read_name_left(rect, b0)  # 닉네임만 따로 크게 잘라서 다시 읽기
                if guess:
                    name_parts = [guess]
            msg = " ".join(msg_parts).strip()
            msg = re.sub(r"^.{0,6}?[:：;]\s*", "", msg)            # 배지 뒤 콜론까지 (귀 아이콘 찌꺼기 포함) 제거
            msg = re.sub(r"^[|!\\/()\[\]{}<>.,'`~]+\s*", "", msg)  # 앞에 남은 기호 찌꺼기
            msg = re.sub(r"^\S{1,2}\s+(?=[SBTL]\s?>)", "", msg).strip()
            badges.append((len(out), red, rect))
            out.append([" ".join(name_parts).strip(), rect, ws, msg])
        # 배지 글씨(채널 번호)는 따로 모아서 한 번에 OCR
        chans = self._read_channels([(red, rect) for _i, red, rect in badges]) if badges else []
        for (idx, _red, _rect), ch in zip(badges, chans):
            name, rect, ws, msg = out[idx]
            out[idx] = (f"{name}\x01{ch}\x01{msg}", rect, ws)
        return out

    def _read_name_left(self, rect, b0):
        """배지 왼쪽(닉네임 자리)만 잘라서 확대 + 흑백 대비를 높여 다시 OCR."""
        try:
            from PIL import Image, ImageOps
            import asyncio
            import winocr
            x, y, w, h = rect
            crop = self._orig.crop((0, max(0, y - 1), max(1, b0 - 1), min(self._orig.height, y + h + 1)))
            gray = ImageOps.autocontrast(crop.convert("L"))
            # 밝은 글씨(어두운 배경)든 어두운 글씨(밝은 배경)든 '검은 글씨/흰 배경'으로 맞춤
            if sum(gray.getdata()) / max(1, gray.width * gray.height) < 128:
                gray = ImageOps.invert(gray)
            big = gray.resize((gray.width * 4, gray.height * 4), Image.LANCZOS).convert("RGB")
            pad = Image.new("RGB", (big.width + 40, big.height + 30), (255, 255, 255))
            pad.paste(big, (20, 15))
            res = asyncio.run(winocr.to_coroutine(winocr.recognize_pil(pad, "en")))
            t = " ".join(ln.text for ln in (getattr(res, "lines", None) or [])).strip()
            t = re.sub(r"[^A-Za-z0-9_ ]", "", t).strip()
            t = re.sub(r"^[Il1|](?=[A-Z])", "", t)  # 왼쪽 테두리를 I 로 읽은 것 제거
            dlog("NAME-RETRY", t)
            return t if len(t) >= 3 else None
        except Exception as e:  # noqa: BLE001
            dlog("NAME-RETRY-ERR", repr(e))
            return None

    def _read_channels(self, items):
        from PIL import Image
        try:
            scale = 4
            crops = []
            for (g0, g1), (x, y, w, h) in items:
                c = self._orig.crop((max(0, g0 - 2), max(0, y), g1 + 3, y + h))
                px = c.load()
                for cx in range(c.width):
                    for cy in range(c.height):
                        px[cx, cy] = (0, 0, 0) if self._is_badge_red(px[cx, cy]) else (255, 255, 255)
                crops.append(c.resize((c.width * scale, c.height * scale), Image.NEAREST))
            gap = 12
            W = max(c.width for c in crops) + 20
            H = sum(c.height + gap for c in crops) + gap
            sheet = Image.new("RGB", (W, H), (255, 255, 255))
            ys, yy = [], gap
            for c in crops:
                sheet.paste(c, (10, yy))
                ys.append((yy, yy + c.height))
                yy += c.height + gap
            if DEBUG[0] and time.time() - getattr(self, "_badge_dbg", 0) > 30:
                self._badge_dbg = time.time()
                try:
                    sheet.save(os.path.join(APP_DIR, "debug_badge.png"))
                    self._orig.save(os.path.join(APP_DIR, "debug_capture.png"))
                except Exception:  # noqa: BLE001
                    pass
            import asyncio
            import winocr
            res = asyncio.run(winocr.to_coroutine(winocr.recognize_pil(sheet, "en")))
            got = ["" for _ in crops]
            for ln in getattr(res, "lines", None) or []:
                for w in ln.words:
                    cy = w.bounding_rect.y + w.bounding_rect.height / 2
                    for i, (a, b) in enumerate(ys):
                        if a - gap / 2 <= cy <= b + gap / 2:
                            got[i] += w.text
            out = []
            for t in got:
                t = re.sub(r"[^A-Za-z0-9]", "", t)
                t = re.sub(r"^[CcGg]?[Hh]", "", t)
                digits = t.translate(_CH_FIX).replace("g", "9").replace("q", "9")
                digits = re.sub(r"\D", "", digits)[-2:]
                m = re.match(r"(\d{1,2})$", digits)
                out.append("CH" + m.group(1).zfill(2) if m else "")
            dlog("BADGE", got, out)
            return out
        except Exception as e:  # noqa: BLE001
            dlog("BADGE-ERR", repr(e))
            return ["" for _ in items]

    @staticmethod
    def _white_mask(img, strict=False):
        """흰색(밝고 무채색) 글자만 검은색으로, 나머지는 흰 배경으로.
        strict=True: 거의 순백색(밝기 215+, 색 차이 작음)만 -> 밝은 맵 배경(하늘·눈·바위) 섞임 줄이기."""
        from PIL import ImageChops
        r, g, b = img.convert("RGB").split()
        mn = ImageChops.darker(ImageChops.darker(r, g), b)
        if not strict:
            return mn.point(lambda v: 0 if v > 150 else 255).convert("RGB")
        mx = ImageChops.lighter(ImageChops.lighter(r, g), b)
        sat = ImageChops.subtract(mx, mn)
        bright = mn.point(lambda v: 255 if v > 215 else 0)
        gray = sat.point(lambda v: 255 if v < 40 else 0)
        m = ImageChops.darker(bright, gray)
        return m.point(lambda v: 0 if v else 255).convert("RGB")

    @staticmethod
    def _color_mask(img, colors, tol=48):
        """지정한 글자색(파티 분홍·귓속말 초록·길드 보라·친구 주황) 픽셀만 검은색으로.
        투명한 채팅창 뒤로 맵이 비쳐도 색깔 글자만 깨끗하게 뽑아서 읽으려고."""
        from PIL import Image, ImageChops
        rgb = img.convert("RGB")
        acc = None
        for c in colors:
            d = ImageChops.difference(rgb, Image.new("RGB", rgb.size, c))
            r, g, b = d.split()
            mx = ImageChops.lighter(ImageChops.lighter(r, g), b)
            hit = mx.point(lambda v: 255 if v <= tol else 0)
            acc = hit if acc is None else ImageChops.lighter(acc, hit)
        # 확성기 분홍 배경처럼 색이 비슷한 '배경 덩어리'는 지움 (가로줄의 40% 넘게 칠해진 줄 = 배경)
        w, h = acc.size
        from PIL import ImageDraw
        dr = ImageDraw.Draw(acc)
        for y in range(h):
            if acc.crop((0, y, w, y + 1)).histogram()[255] > w * 0.4:
                dr.line([(0, y), (w, y)], fill=0)
        return acc.point(lambda v: 0 if v else 255).convert("RGB")

    @staticmethod
    def _yellow_mask(img):
        """노란 아이템 링크 글자([Chocolate Label Ring] 같은 것)만 검은색으로.
        고확·확성기 배경(진한 노랑/주황) 위에서 영어 OCR이 아이템 이름을 통째로 빼먹는 문제 해결."""
        from PIL import ImageChops
        r, g, b = img.convert("RGB").split()
        ro = r.point(lambda v: 255 if v > 225 else 0)
        go = g.point(lambda v: 255 if v > 225 else 0)
        bo = b.point(lambda v: 255 if 80 < v < 215 else 0)
        m = ImageChops.darker(ImageChops.darker(ro, go), bo)
        return m.point(lambda v: 0 if v else 255).convert("RGB")

    @staticmethod
    def _add_missing_words(en, extra):
        """다른 방법으로 읽은 단어(extra) 중 원래 줄에 없는 것을 같은 줄 같은 위치에 끼워 넣음."""
        out = []
        used = set()
        for text, rect, ws in en:
            if not rect or not ws:
                out.append((text, rect, ws))
                continue
            cy = rect[1] + rect[3] / 2
            add = []
            for j, (t2, r2, w2) in enumerate(extra):
                if j in used or not r2 or not w2:
                    continue
                if abs((r2[1] + r2[3] / 2) - cy) > max(rect[3], r2[3]) * 0.6:
                    continue
                used.add(j)
                low = normalize(text)
                # 아이템 링크는 항상 [ ] 안에 있음 -> 대괄호 안 단어만 씀 (맵의 노란 바위 같은 찌꺼기 방지)
                inside, keep = False, []
                for w in w2:
                    if "[" in w[0]:
                        inside = True
                    if inside:
                        keep.append(w)
                    if "]" in w[0]:
                        inside = False
                for w in keep:
                    if len(re.sub(r"[^A-Za-z0-9]", "", w[0])) < 2 and not re.search(r"[\[\]]", w[0]):
                        continue
                    wx0, wx1 = w[1], w[1] + w[3]
                    overlap = any(min(wx1, o[1] + o[3]) - max(wx0, o[1]) > w[3] * 0.4 for o in ws)
                    if not overlap and normalize(w[0]) not in low:
                        add.append(w)
            if add:
                nws = sorted(list(ws) + add, key=lambda w: w[1])
                ntext = " ".join(w[0] for w in nws)
                dlog("YELLOW-ADD", text, "=>", ntext)
                x0 = min(w[1] for w in nws); y0 = min(w[2] for w in nws)
                x1 = max(w[1] + w[3] for w in nws); y1 = max(w[2] + w[4] for w in nws)
                out.append((ntext, (int(x0), int(y0), int(x1 - x0), int(y1 - y0)), nws))
            else:
                out.append((text, rect, ws))
        # 원래 줄이 아예 없는 곳에서 읽힌 노란 글자 줄은 버림 (공지 등과 섞이지 않게)
        return out

    @staticmethod
    def _pick_better(en, en2):
        out = []
        used = set()
        for text, rect, ws in en:
            best = (text, rect, ws)
            if rect:
                cy = rect[1] + rect[3] / 2
                for j, (t2, r2, w2) in enumerate(en2):
                    if j in used or not r2:
                        continue
                    if abs((r2[1] + r2[3] / 2) - cy) <= max(rect[3], r2[3]) * 0.6:
                        if text_quality(t2) > text_quality(text) + 0.1 and len(t2) >= len(text) * 0.6:
                            best = (t2, rect, w2)
                        used.add(j)
                        break
            out.append(best)
        return out

    def _ko_supported(self):
        if self._ko is None:
            try:
                from winrt.windows.globalization import Language
                from winrt.windows.media.ocr import OcrEngine
                self._ko = bool(OcrEngine.is_language_supported(Language("ko")))
            except Exception:  # noqa: BLE001
                self._ko = False
        return self._ko

    def read_lines(self, region):
        """반환: [(줄 텍스트, 좌표(x,y,w,h) 또는 None, [한글 아이템 링크들])]
        영어 OCR로 채팅을 읽고, 한국어 OCR을 한 번 더 돌려서
        [블루 샤크] 같은 한글 아이템 이름을 같은 줄에 붙여줌."""
        img = self.grab(region)
        self.last_img = self._orig
        en = self._ocr(img, self.lang)
        ko = []
        if self.lang != "ko" and not getattr(self, "_no_en", False) and self._ko_supported():
            try:
                ko = self._ocr(img, "ko")
            except Exception:  # noqa: BLE001
                ko = []
        # 일반 채팅(흰 글씨)은 '흰 글씨만 검은색으로 뽑은 그림'으로 한 번 더 읽어서 더 깨끗한 쪽을 씀
        if self.lang != "ko":
            try:
                wm = self._white_mask(img)
                en2 = self._ocr(wm, self.lang)
                en = self._pick_better(en, en2)
                # 채팅창이 투명해서 밝은 맵 배경이 비칠 때: 더 엄격하게 '진짜 흰색'만 뽑아서 한 번 더
                wm2 = self._white_mask(img, strict=True)
                en = self._pick_better(en, self._ocr(wm2, self.lang))
            except Exception as e:  # noqa: BLE001
                dlog("WHITE-ERR", repr(e))
            try:
                cm = self._color_mask(img, [c for k, c, _ in TEXT_KINDS if k != "공지"])
                if cm.convert("L").histogram()[0] > 200:  # 색깔 글자(파티·귓속말 등)가 있을 때만
                    cl = self._ocr(cm, self.lang)
                    en = self._pick_better(en, cl)
                    # 원래 OCR이 아예 못 찾은 색깔 줄(맵 배경에 묻힌 파티 채팅 등)은 새 줄로 추가
                    for t2, r2, w2 in cl:
                        if not r2 or text_quality(t2) < 0.6:
                            continue
                        y0, y1 = r2[1], r2[1] + r2[3]

                        def ov(r):
                            return r and min(y1, r[1] + r[3]) - max(y0, r[1]) > r2[3] * 0.5
                        same = [x for x in en if ov(x[1])]
                        if any(x[0] == t2 for x in same):
                            continue
                        q2 = text_quality(t2)
                        if not same or all(text_quality(x[0]) < min(0.45, q2 - 0.15) or
                                           text_quality(x[0]) < q2 - 0.25 for x in same):
                            # 겹치는 원래 줄이 없거나 다 찌꺼기면 -> 색깔 글자로 읽은 줄로 바꿈
                            dlog("COLOR-ADD", t2, "replaces", [x[0] for x in same])
                            en = [x for x in en if x not in same] + [(t2, r2, w2)]
                    en.sort(key=lambda x: (x[1][1] if x[1] else 0))
            except Exception as e:  # noqa: BLE001
                dlog("COLORMASK-ERR", repr(e))
            try:
                ym = self._yellow_mask(img)
                if ym.convert("L").getextrema()[0] < 128:  # 노란 글자가 있을 때만 한 번 더 읽음
                    en = self._add_missing_words(en, self._ocr(ym, self.lang))
            except Exception as e:  # noqa: BLE001
                dlog("YELLOW-ERR", repr(e))
        dlog("OCR-EN", [t[0] for t in en])
        dlog("OCR-KO", [t[0] for t in ko])
        en = self._apply_badges(img, en)
        out = []
        for text, rect, _ws in en:
            items = []
            if rect and ko:
                cy = rect[1] + rect[3] / 2
                for ktext, krect, _kw in ko:
                    if not krect:
                        continue
                    kcy = krect[1] + krect[3] / 2
                    if abs(kcy - cy) <= max(rect[3], krect[3]) * 0.6:
                        # 한글로 된 줄(공지·한국인 채팅)은 영어 OCR이 찌꺼기로 읽으니 한글 OCR 결과를 그대로 사용
                        plain_k = re.sub(r"\[[^\]]*\]", "", ktext)
                        han = len(HANGUL_RE.findall(plain_k))
                        chars = len(re.sub(r"\s", "", plain_k)) or 1
                        if han >= 4 and han / chars > 0.45 and "\x01" not in text:
                            text, items = ktext.strip(), []
                            break
                        found = re.findall(r"\[[^\[\]]*[가-힣][^\[\]]*\]", ktext)
                        if not found and re.search(r"[\[\]]", text):
                            # 대괄호를 못 읽은 경우: 영어 줄에 아이템 링크 흔적([ ])이 있을 때만
                            # 한글 덩어리를 아이템 이름으로 (아이콘·특수문자를 한글로 잘못 읽은 찌꺼기 방지)
                            found = ["[" + h.strip() + "]" for h in
                                     re.findall(r"[가-힣][가-힣0-9 +\-]*[가-힣]", ktext)]
                        bad = [f for f in found if not _looks_like_item(f)]
                        if bad:
                            report_issue("아이템 이름이 깨짐", text + "  /  한글OCR: " + ktext, img=self._orig, rect=rect)
                        found = [f for f in found if _looks_like_item(f)]
                        items += [f for f in found if f not in items]
            out.append((text, rect, items))
        return out


# 아이템 이름에 거의 안 나오는 받침 조합 (OCR이 아이콘을 한글로 잘못 읽을 때 자주 생김)
_RARE_FINALS = set("ㄳㄵㄶㄺㄻㄼㄽㄾㄿㅀㅄㅋㅌㅍㅎㅊㅈㅅㅆ")
_FINALS = [""] + list("ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ")


def _looks_like_item(item):
    """[녛쐈] [궂무춘과] 같은 OCR 찌꺼기를 걸러냄."""
    syl = [c for c in item if "가" <= c <= "힣"]
    if len(syl) < 2:
        return False
    rare = sum(1 for c in syl if _FINALS[(ord(c) - 0xAC00) % 28] in _RARE_FINALS)
    double = sum(1 for c in syl if (ord(c) - 0xAC00) // 588 in (1, 4, 8, 10, 13))  # ㄲㄸㅃㅆㅉ 초성
    return (rare + double) / len(syl) < 0.34


# OCR이 S> B> 를 S) 5> B) 8> S» 처럼 잘못 읽는 경우를 원래대로 되돌림
_TRADE_TAG_RE = re.compile(r"(?:(?<=^)|(?<=[\s:;/,\]]))([SsBbTtLlFfJj5$8])\s?[>)»›\}<]\s*")
_TRADE_MAP = {"5": "S", "$": "S", "8": "B"}


def normalize_trade(msg):
    # ": S>" 의 콜론을 K/I 로 읽어서 "KS>" "IS>" 가 되는 경우
    msg = re.sub(r"(?:(?<=\s)|^)[KkIl|:;.][SB]\s*>", lambda m: m.group(0)[1:], msg)
    # 금액 앞 숫자 1 을 l / I 로 읽은 경우: "l .8m" -> "1.8m", "I mil" -> "1 mil"
    msg = re.sub(r"(?<=\.\d)rn(?![A-Za-z])", "m", msg)  # "I .5rn" -> "I .5m" (m 을 rn 으로 읽음)
    msg = re.sub(r"(?<![A-Za-z0-9])[lI|]\s?\.\s?(\d+)\s?((?i:k|m|mil))(?![A-Za-z])", r"1.\1\2", msg)
    msg = re.sub(r"(?<=[Tt]his)[lI|]\s?\.\s?(\d+)\s?((?i:k|m|mil))(?![A-Za-z])", r" 1.\1\2", msg)  # "Thisl .3M" -> This 1.3M
    msg = re.sub(r"(?<=\d[kKmM])(?=[SBT]>)", " ", msg)  # "70kS>Earring" -> 70k S>Earring (금액이 안 읽히던 문제)
    msg = re.sub(r"(?<=\d)\s?magt(?![A-Za-z])", " matt", msg, flags=re.I)  # "20magt" -> 20 matt (마력)
    # 금액 사이 끼어든 공백: "1 .7m" / "1. 1k" -> 1.7m / 1.1k, "21 0k" / "2 00k" -> 210k / 200k
    msg = re.sub(r"(?<![A-Za-z0-9.,])(\d{1,3})\s\.(\d{1,2})\s?((?i:k|m|mil))(?![A-Za-z])", r"\1.\2\3", msg)
    msg = re.sub(r"(?<![A-Za-z0-9.,])(\d{1,3})\.\s(\d)((?i:k|m|mil))(?![A-Za-z])", r"\1.\2\3", msg)
    msg = re.sub(r"(?<![A-Za-z0-9.,])(\d{1,3})\s(0{1,2})\s?((?i:k))(?![A-Za-z])", r"\1\2\3", msg)
    # 60% 를 600/0 으로 읽은 경우 (GFA600/0 -> GFA 60%)
    msg = re.sub(r"(?<![\d/])(10|30|60|70|100)0/0(?![\d])", r" \1%", msg)
    msg = re.sub(r"(?<![\d/.,])1\s00/0(?![\d])", " 10%", msg)  # "LUK 1 00/0" -> 10%
    msg = re.sub(r"(?<=[A-Za-z\s])G[O0]{2}/0(?![\d])", " 60%", msg)  # "SCROLLGOO/0" -> 60%
    msg = re.sub(r"(?<=[A-Za-z])[Il|f]\s?[O0]{2}\s?%", " 100%", msg)  # "SCROLLIOO%" -> SCROLL 100%
    # % 를 70 으로 읽은 경우: "Speed 6070" "1070 LUK" "ACC6070 20" -> 60% / 10%
    #   "6070145k" -> "60% 145k" (60억으로 읽히던 문제)
    msg = re.sub(r"(?<![\d/.,+$])(10|60)70(\d{2,3}(?:\.\d)?\s?[kK])(?![A-Za-z])", r" \1% \2", msg)
    msg = re.sub(r"(?<![\d/.,+$])(10|60)7\.\s?(\d{2,3}\s?[kK])(?![A-Za-z])", r" \1% \2", msg)  # "607.45k" -> 60% 45k
    msg = re.sub(r"(?<![\d/.,+$])(10|60)70(?=\s|$|[.,!@)]|(?i:luk|str|dex|int)(?![A-Za-z]))", r" \1% ", msg)
    msg = re.sub(r"(?<=\d)km(?![A-Za-z])", "k", msg)  # "50km" -> 50k
    msg = re.sub(r"(?<![A-Za-z0-9.])(\d{1,3}(?:\.\d)?[kK])[l|I](?=\s|$|[A-Za-z])", r"\1 / ", msg)  # "40klslime" -> 40k / slime
    msg = re.sub(r"(?<![A-Za-z0-9.])(\d{1,2})\s?M[lI1|]N(?:S)?(?![A-Za-z])", r"\1 min", msg)  # "5MlN" -> 5 min (500만 아님)
    msg = re.sub(r"(?<=[A-Za-z]{2})(\d{1,3}(?:\.\d)?[kK])(?![A-Za-z0-9])", r" \1", msg)  # "DEF30k" -> DEF 30k
    msg = re.sub(r"(?<![A-Za-z0-9.+])(\d{1,3})\}<(?=\s|[-—–@]|$)", r"\1k", msg)  # "10}<" -> 10k
    # 10 을 IO / 1 O 로 읽은 경우: "+IO" "IOLuk" "1 Oint 2cd" -> +10 / 10Luk / 10 INT
    msg = re.sub(r"\+\s?[Il][O0](?![A-Za-z0-9])", "+10", msg)
    msg = re.sub(r"(?<![A-Za-z0-9])[1Il][O0](?=\s?(?i:luk|str|dex|int|att|atk)(?![A-Za-z]))", "10", msg)
    msg = re.sub(r"(?<![A-Za-z0-9])[1Il]\s[O0]int(?![A-Za-z])", "10 INT", msg, flags=re.I)
    # IOK / I OOk / lOOk -> 10K / 100k (1 을 I·l, 0 을 대문자 O 로 읽음)
    msg = re.sub(r"(?<![A-Za-z0-9])([1-9Il])\s?([O0]{1,2})\s?([kK])(?![A-Za-z])",
                 lambda m: m.group(1).replace("I", "1").replace("l", "1") + m.group(2).replace("O", "0") + m.group(3), msg)
    msg = re.sub(r"(?<=\d)([kK])(ea|each|mesos?)(?![A-Za-z])", r"\1 \2", msg, flags=re.I)  # 150kea -> 150k ea
    msg = re.sub(r"(?<=\d)L[lI|1]JK(?![A-Za-z])", "LUK", msg)  # 8LlJK -> 8LUK
    msg = re.sub(r"(?<![A-Za-z])LCK(?![A-Za-z])", "LUK", msg)  # 8LCK / +8 LCK earring -> LUK (운)
    msg = re.sub(r"(?<=\d)CR[lI|1]T(?![a-z])", "CRIT", msg)  # 6CRlT6ATK -> 6CRIT6ATK
    msg = re.sub(r"(?<![A-Za-z])CRIT[I|l1]{2,}(?![A-Za-z0-9])", "CRIT", msg)  # +2 CRITIII -> +2 CRIT
    msg = re.sub(r"\+\s?[Il|](?=\s?(?i:w?att|w?atk|str|dex|int|luk)(?![A-Za-z]))", "+1", msg)  # +IWATT -> +1WATT
    msg = re.sub(r"(?<=\d)\s?cr[il1|]t\.?\s?(?:dmg|damage)(?![A-Za-z])", "CD", msg, flags=re.I)  # 3CRlT DMG -> 3CD (크뎀)
    msg = re.sub(r"(?<![A-Za-z0-9])(\d)1uk(?![A-Za-z])", r"\1luk", msg, flags=re.I)  # 51uk -> 5luk
    msg = re.sub(r"(?<![A-Za-z])[Il][Vv][Il]\s?(?=\d)", "lvl ", msg)  # IVI 50 -> lvl 50
    msg = re.sub(r"(?<![A-Za-z])FM\s?(?=(?:ch|cc)\.?\s?\d)", "FM ", msg, flags=re.I)  # FMch27 -> FM ch27
    msg = re.sub(r"(?<![A-Za-z0-9])[lI|]\s(mil|mill|million)(?![A-Za-z])", r"1 \1", msg)
    # OCR: k 를 '1<' 로 읽음 "651<—" -> "65k—" (하트 <3 은 제외)
    msg = re.sub(r"(?<![A-Za-z0-9.+])(\d{1,3})1<(?=\s|[-—–]|$)", r"\1k", msg)
    # 가격 범위 "15-45k" -> "15k-45k" (앞 숫자가 1.5만이 아니라 15 로 남지 않게)
    def _range(m):
        a, b, u = m.group(1), m.group(2), m.group(3)
        return f"{a}{u}-{b}{u}" if float(a) < float(b) else m.group(0)
    msg = re.sub(r"(?<![A-Za-z0-9.+\-])(\d{1,3}(?:\.\d)?)\s?-\s?(\d{1,3}(?:\.\d)?)\s?(k|m)(?![A-Za-z])", _range, msg, flags=re.I)

    def fix(m):
        before = msg[:m.start()]
        if ">" not in m.group(0) and before.count("(") > before.count(")"):
            return m.group(0)  # "(+3 B)" 처럼 괄호 안이면 거래 표시가 아님
        c = m.group(1)
        return _TRADE_MAP.get(c, c.upper()) + "> "
    return _TRADE_TAG_RE.sub(fix, msg)


LOG_PATH = os.path.join(APP_DIR, "debug_log.txt")


def dlog(*parts):
    """문제 확인용 기록 (config.json 의 "debug": true 일 때만)."""
    if not DEBUG[0]:
        return
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 2_000_000:
            os.remove(LOG_PATH)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + " | ".join(str(p) for p in parts) + "\n")
    except Exception:  # noqa: BLE001
        pass


DEBUG = [False]
OCR_NOTICE = [None]  # 화면에 한 번 알려줄 OCR 관련 안내


# ---- 경험치 측정: 화면 아래 경험치 숫자를 읽어서 시간당 경험치 계산 ----
EXP_LOG_PATH = os.path.join(APP_DIR, "exp_log.csv")
# 레벨별 필요 경험치 (클래식 월드, meowdb EXP 표). EXP_TABLE[레벨] = 그 레벨에서 다음 레벨까지 필요한 경험치
EXP_TABLE = [0] + [15, 34, 57, 92, 135, 372, 560, 840, 1242, 1716, 2360, 3216, 4200, 5460, 7050, 8840, 11040, 13716, 16680, 20216, 24402, 28980, 34320, 40512, 47216, 54900, 63666, 73080, 83720, 95700, 108480, 122760, 138666, 155540, 174216, 194832, 216600, 240500, 266682, 294216, 324240, 356916, 391160, 428280, 468450, 510420, 555680, 604416, 655200, 709716, 748608, 789631, 832902, 878545, 926689, 977471, 1031036, 1087536, 1147132, 1209994, 1276301, 1346242, 1420016, 1497832, 1579913, 1666492, 1757815, 1854143, 1955750, 2062925, 2175973, 2295216, 2420993, 2553663, 2693603, 2841212, 2996910, 3161140, 3334370, 3517093, 3709829, 3913127, 4127566, 4353756, 4592341, 4844001, 5109452, 5389449, 5684790, 11938059, 25069923, 52646838, 110558359, 232172553, 487562361, 1023880958, 2150150011, 4515315023, 9482161548]


_EXP_PCT_RE = re.compile(r"(\d{1,4})[.,](\d{2})\s*[%)\]]")
_EXP_RATIO_RE = re.compile(r"(\d[\d,.]*)\s*/\s*(\d[\d,.]*)")
_EXP_NUM_RE = re.compile(r"\d{1,3}(?:[,.]\d{3})+|\d{3,}")


def _exp_int(s):
    s = re.sub(r"[^\d]", "", s or "")
    return int(s) if s else None


def parse_exp(text):
    """경험치 줄 글자에서 {"cur": 현재 경험치(틀릴 수 있음), "need": 필요 경험치 또는 None, "pct": 퍼센트} 를 뽑음.
    클래식 월드 표기: 'EXP 175,988 [44.99%]'. 퍼센트가 제일 정확하게 읽혀서 퍼센트를 기준으로 씀."""
    if not text:
        return None
    t = text
    # OCR 이 숫자를 글자로 잘못 읽는 경우 (숫자 옆에 붙은 것만): O->0, l/I->1, S->5
    for _ in range(5):
        t2 = re.sub(r"(?<=[\d,.])[Oo](?=[\d,.%Oo\s\]\)/]|$)|(?<=[\s\[(])[Oo](?=[.,]\d)", "0", t)
        t2 = re.sub(r"(?<=[\d,.])[lI|](?=[\d,.%lI|\s\]\)/]|$)", "1", t2)
        t2 = re.sub(r"(?<=[\d,.])[Ss](?=[\d,.%Ss\s\]\)/]|$)|(?<=[\s\[(])[Ss](?=[\d.,])", "5", t2)
        if t2 == t:
            break
        t = t2
    pct = None
    m = None
    for m in _EXP_PCT_RE.finditer(t):
        whole, dec = m.group(1), m.group(2)
        v = float(whole + "." + dec)
        while v > 100 and len(whole) > 1:   # '[44.99%' 의 '[' 를 1로 읽은 경우 -> 앞자리 버림
            whole = whole[1:]
            v = float(whole + "." + dec)
        if 0 <= v <= 100:
            pct = v
            break
    if pct is None:
        return None
    before = t[:m.start()]
    cur = need = None
    r = _EXP_RATIO_RE.search(before)
    if r:
        cur, need = _exp_int(r.group(1)), _exp_int(r.group(2))
        if need and cur is not None and cur > need:
            cur = need = None
    if cur is None:
        nums = [_exp_int(x) for x in _EXP_NUM_RE.findall(before)]
        nums = [n for n in nums if n is not None]
        if nums:
            cur = nums[-1]
    return {"cur": cur, "need": need, "pct": pct}


class ExpTracker:
    """경험치 읽은 값을 받아서 누적 경험치·시간당 경험치를 계산.
    퍼센트(소수 둘째 자리)를 기준으로 계산하고, 숫자는 퍼센트와 맞을 때만 씀 (OCR 이 3/5 를 자주 헷갈림)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.running = False
        self.elapsed = 0.0
        self.t_start = None
        self.gained = 0          # 필요 경험치를 알게 된 뒤의 누적 경험치
        self.gained_pct = 0.0
        self.unpriced = 0.0      # 필요 경험치를 아직 몰라서 경험치로 못 바꾼 퍼센트
        self.levelups = 0
        self.last = None         # {"cur","need","pct"} (cur/need 는 보정된 값)
        self.odd = []
        self.cands = []          # 필요 경험치 후보 (현재 경험치 ÷ 퍼센트)
        self.need = None
        self.level = None        # 레벨 표로 알아낸 현재 레벨
        self.lvl_wait = []       # 레벨업 확인 대기 중인 퍼센트들
        self.fwd = None          # 크게 오른 값 확인 대기
        self.last_ok = None
        self.fails = 0

    def start(self):
        if not self.running:
            self.running = True
            self.t_start = time.time()
            self.last = None   # 멈춘 동안 오른 경험치는 안 셈

    def pause(self):
        if self.running:
            self.elapsed += time.time() - self.t_start
            self.running = False

    def total_time(self):
        return self.elapsed + (time.time() - self.t_start if self.running and self.t_start else 0)

    def _learn_need(self, rd):
        """현재 경험치 ÷ 퍼센트로 필요 경험치를 추정 -> 레벨 표에서 가장 가까운 레벨로 맞춤."""
        if self.level:
            return
        c, p = rd.get("cur"), rd["pct"]
        cand = rd.get("need") or (c * 100 / p if c and c >= 100 and p >= 1 else None)
        if not cand:
            return
        lv = None
        for i in range(1, len(EXP_TABLE)):
            if abs(EXP_TABLE[i] - cand) <= EXP_TABLE[i] * 0.01:
                lv = i
                break
        self.cands = (self.cands + [lv])[-20:]
        votes = {}
        for x in self.cands:
            if x:
                votes[x] = votes.get(x, 0) + 1
        if votes:
            best = max(votes, key=votes.get)
            if votes[best] >= 2:
                self.set_level(best)

    def set_level(self, lv):
        self.level = lv
        self.need = EXP_TABLE[lv] if 0 < lv < len(EXP_TABLE) else None
        if self.need and self.unpriced:
            self.gained += int(round(self.need * self.unpriced / 100))
            self.unpriced = 0.0
        if self.last and self.need:
            self.last["need"] = self.need

    def _confirm_levelup(self, rd):
        """퍼센트가 크게 떨어졌을 때 진짜 레벨업인지 확인.
        - 다음 레벨 필요 경험치로 계산한 숫자와 읽은 숫자가 맞으면 바로 인정
        - 아니면 낮은 퍼센트가 3번 연속 비슷하게 읽혀야 인정 ('69.82%'를 '9.82%'로 잘못 읽은 것 걸러냄)"""
        p, c = rd["pct"], rd.get("cur")
        if self.level and 0 < self.level + 1 < len(EXP_TABLE) and c is not None and p >= 0.3:
            nxt = EXP_TABLE[self.level + 1]
            if abs(c - nxt * p / 100) <= max(80, nxt * 0.002):
                return True
        if not hasattr(self, "lvl_wait"):
            self.lvl_wait = []
        if c is not None and self.need and c > self.need * 0.06:
            return False  # 숫자는 여전히 큼 -> 퍼센트만 잘못 읽은 것
        self.lvl_wait.append(p)
        w = self.lvl_wait[-3:]
        if len(w) >= 3 and max(w) - min(w) <= 1.0:
            return True
        return False

    def _cur(self, rd):
        """퍼센트와 맞는 현재 경험치 (숫자를 잘못 읽었으면 퍼센트로 계산)."""
        if not self.need:
            return None
        exp_c = self.need * rd["pct"] / 100
        c = rd.get("cur")
        if c is not None and abs(c - exp_c) <= max(60, self.need * 0.0015):
            return c
        return int(round(exp_c))

    def feed(self, rd):
        """읽은 값 하나 반영. 퍼센트와 숫자(현재 경험치÷필요 경험치) 두 가지 후보 중
        직전 값에서 자연스럽게 이어지는 쪽을 씀 -> '75.01%'를 '8.01%'로 잘못 읽어도 숫자로 바로잡음."""
        if not self.running:
            return
        if not rd or rd.get("pct") is None:
            self.fails += 1
            return
        self.fails = 0
        self.last_ok = time.time()
        self._learn_need(rd)
        p_read, c = rd["pct"], rd.get("cur")
        cands = [p_read]
        if self.need and c is not None and 0 <= c <= self.need and c >= 1000:
            cands.append(c * 100 / self.need)
        if self.last is None:
            # 처음 기준값: 비슷한 값이 3번 읽혀야 정함 (첫 값을 잘못 읽으면 계속 틀어지니까)
            agree = len(cands) == 2 and abs(cands[0] - cands[1]) <= 0.3
            v = cands[1] if agree else p_read
            self.odd = [x for x in self.odd if isinstance(x, float)][-4:] + [float(v)]
            near = [x for x in self.odd if abs(x - v) <= 0.3]
            if len(near) >= 3:
                self.last = {"pct": max(near), "need": self.need}
                self.odd = []
            return
        lp = self.last["pct"]
        # 1) 평소: 직전 값에서 0~1% 사이로 오른 후보 (몇 초 사이에 1% 넘게 오를 일은 없음)
        ok = [x for x in cands if -0.03 <= x - lp <= 1.0]
        if ok:
            p = min(ok, key=lambda x: abs(x - lp - 0.05))
            if p - lp > 0.15:
                # 한 번에 0.15% 넘게 오름 = 드묾 -> 다음 값도 그만큼 높게 읽혀야 인정 (숫자 오독 방지)
                fw = getattr(self, "fwd", None)
                self.fwd = p
                if fw is None or not any(abs(x - fw) <= 0.1 or x > fw for x in cands):
                    return
            self.fwd = None
            dp = max(0.0, p - lp)
            self.gained_pct += dp
            if self.need:
                self.gained += int(round(self.need * dp / 100))
            else:
                self.unpriced += dp
            self.last = {"pct": max(p, lp), "need": self.need}
            self.odd, self.lvl_wait = [], []
            return
        # 2) 레벨업: 직전이 높고(85%+) 지금이 아주 낮음(5% 미만) + 확인
        if lp >= 85 and p_read < 5 and self._confirm_levelup(rd):
            self.gained_pct += (100 - lp) + p_read
            if self.need:
                self.gained += int(round(self.need * (100 - lp) / 100))
            else:
                self.unpriced += 100 - lp
            self.levelups += 1
            self.cands, self.odd, self.lvl_wait = [], [], []
            if self.level:
                self.set_level(self.level + 1)   # 다음 레벨 필요 경험치는 표에서 바로
                if self.need:
                    self.gained += int(round(self.need * p_read / 100))
                else:
                    self.unpriced += p_read
            else:
                self.need = None
                self.unpriced += p_read
            self.last = {"pct": p_read, "need": self.need}
            return
        # 3) 그 밖의 튀는 값 = 잘못 읽음 -> 무시. 단, 같은 값이 5번 연속(죽어서 경험치가 깎임 등)이면 새 기준으로
        self.odd.append(p_read)
        w = self.odd[-5:]
        if len(w) >= 5 and max(w) - min(w) <= 0.3 and all(abs(x - p_read) <= 0.5 for x in cands):
            # 같은 값이 계속 읽힘 = 진짜 값. 그 사이 못 읽은 동안 오른 만큼(또는 잘못 높게 센 만큼)을 반영
            # (마우스가 숫자를 가렸다가 비켜났을 때, 죽어서 경험치가 깎였을 때 등)
            delta = p_read - lp
            self.gained_pct = max(0.0, self.gained_pct + delta)
            if self.need:
                self.gained = max(0, self.gained + int(round(self.need * delta / 100)))
            else:
                self.unpriced = max(0.0, self.unpriced + delta)
            self.last = {"pct": p_read, "need": self.need}
            self.odd = []

    def gained_exp(self):
        if self.unpriced and self.need:
            return self.gained + int(round(self.need * self.unpriced / 100))
        return self.gained

    def rate(self):
        t = self.total_time()
        return self.gained_exp() / t * 3600 if t >= 20 else 0

    def pct_rate(self):
        t = self.total_time()
        return self.gained_pct / t * 3600 if t >= 20 else 0

    def eta_sec(self):
        r = self.rate()
        if not r or not self.need or not self.last:
            return None
        left = max(0, self.need * (100 - self.last["pct"]) / 100)
        return left / r * 3600


def fmt_hms(sec):
    sec = int(sec or 0)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def fmt_eta(sec):
    if sec is None:
        return "-"
    m = int(sec // 60)
    return f"{m // 60}시간 {m % 60}분" if m >= 60 else f"{m}분"

# ---- 문제 자동 기록: 이상해 보이는 줄을 스스로 찾아서 모아둠 (나중에 한 번에 고치기용) ----

MARKET_DIR = os.path.join(APP_DIR, "시세기록")
MARKET_DB_PATH = os.path.join(MARKET_DIR, "market.db")  # 시세·거래 저장소 (Claude 브리핑이 분석)


class TradeLog:
    """인게임 팝니다/삽니다 채팅을 시세기록/market.db 에 한 줄씩 기록.
    AI 호출 없음 (토큰 0). 오류가 나도 번역기는 그대로 돌고 기록만 꺼짐."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.db = None
        self.recent = {}  # 같은 사람이 같은 글을 화면 재인식으로 또 읽은 것 거르기 (90초)

    def _open(self):
        import sqlite3
        os.makedirs(MARKET_DIR, exist_ok=True)
        db = sqlite3.connect(MARKET_DB_PATH, timeout=5)
        db.execute("CREATE TABLE IF NOT EXISTS chat_trades(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, side TEXT,"
                   " item_key TEXT, price_text TEXT, repeats INTEGER, raw TEXT, source TEXT)")
        cols = {r[1] for r in db.execute("PRAGMA table_info(chat_trades)")}
        for col in ("name", "channel", "kind", "prices"):
            if col not in cols:
                db.execute(f"ALTER TABLE chat_trades ADD COLUMN {col} TEXT")
        db.commit()
        return db

    def add(self, name, ch, kind, msg):
        if not self.enabled:
            return
        side = trade_of(msg)
        if not side:
            return
        try:
            now = time.time()
            key = ((name or "").lower(), re.sub(r"\W+", "", msg.lower()))
            if now - self.recent.get(key, 0) < 90:
                return
            self.recent[key] = now
            if len(self.recent) > 500:
                for k, t in list(self.recent.items()):
                    if now - t > 90:
                        del self.recent[k]
            if self.db is None:
                self.db = self._open()
            prices = prices_in(msg)
            self.db.execute(
                "INSERT INTO chat_trades(ts, side, item_key, price_text, repeats, raw, source, name, channel, kind, prices)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"), "S" if side == "sell" else "B", None,
                 " ".join(re.findall(r"\d+(?:[.,]\d+)?\s*[kKmM]\b", msg)), 1, msg, "translator live",
                 name, ch, kind, ",".join(str(p) for p in prices)))
            self.db.commit()
        except Exception as e:  # noqa: BLE001
            dlog("TRADELOG-OFF", type(e).__name__, e)
            self.enabled = False  # 기록만 끄고 번역은 계속


class MarketCollector:
    """1시간마다 메소워치(meso.watch) 전체 시세를 시세기록/market.db 에 저장 (개인 분석용).
    AI 호출 없음 (토큰 0). 백그라운드에서 조용히 돌고, 실패하면 다음 시간에 다시 시도."""

    URL = "https://meso.watch/data/list.json"

    def __init__(self, enabled=True, every_sec=3600):
        self.enabled = enabled
        self.every = every_sec
        self.last_ok = None

    def start(self):
        if self.enabled:
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        time.sleep(120)  # 번역기 켜고 2분 뒤 첫 수집
        while True:
            try:
                n = self.collect_once()
                self.last_ok = time.time()
                dlog("MARKET", "saved", n, "items")
            except Exception as e:  # noqa: BLE001
                dlog("MARKET-FAIL", type(e).__name__, e)
            time.sleep(self.every)

    def collect_once(self):
        import sqlite3
        import urllib.request
        req = urllib.request.Request(self.URL, headers={"User-Agent": "Mozilla/5.0 (MapleChatTranslator personal price log)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        items = data if isinstance(data, list) else (data.get("items") or list(data.values()))
        os.makedirs(MARKET_DIR, exist_ok=True)
        db = sqlite3.connect(MARKET_DB_PATH, timeout=10)
        try:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS items(item_id TEXT PRIMARY KEY, name_en TEXT, name_ko TEXT, category TEXT, tier TEXT, note TEXT);"
                "CREATE TABLE IF NOT EXISTS price_snapshots(ts TEXT, item_id TEXT, price REAL, p25 REAL, p75 REAL, asks INTEGER,"
                " sold INTEGER, ask_median REAL, source TEXT, PRIMARY KEY(ts,item_id));"
                "CREATE TABLE IF NOT EXISTS daily_median(date TEXT, item_id TEXT, median REAL, source TEXT, PRIMARY KEY(date,item_id));")
            ts = time.strftime("%Y-%m-%d %H:00")
            n = 0
            for it in items:
                name = it.get("name")
                if not name or it.get("price") is None:
                    continue
                db.execute("INSERT OR IGNORE INTO items(item_id, name_en, category) VALUES(?,?,?)",
                           (name, name, it.get("category")))
                db.execute("INSERT OR IGNORE INTO price_snapshots VALUES(?,?,?,?,?,?,?,?,?)",
                           (ts, name, it.get("price"), it.get("p25"), it.get("p75"), it.get("asks"),
                            it.get("sold"), it.get("askMedian"), "meso.watch"))
                for h in it.get("history") or []:
                    if h.get("date") and h.get("median") is not None:
                        db.execute("INSERT OR IGNORE INTO daily_median VALUES(?,?,?,?)",
                                   (h["date"], name, h["median"], "meso.watch"))
                n += 1
            db.commit()
            return n
        finally:
            db.close()


class MarketImporter:
    """크롬(Claude 브리핑)이 다운로드 폴더에 내려받은 mesowatch_*.json (메소워치 전체 시세)을
    시세기록/market.db 에 넣고, 파일은 시세기록/raw/ 로 옮김. AI 호출 없음 (토큰 0)."""

    def __init__(self, cfg):
        self.cfg = cfg

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def dirs(self):
        home = os.path.expanduser("~")
        out = [self.cfg.get("download_dir") or "", os.path.join(home, "Downloads"), os.path.join(home, "다운로드")]
        return [d for d in dict.fromkeys(out) if d and os.path.isdir(d)]

    def _loop(self):
        time.sleep(20)
        while True:
            try:
                for d in self.dirs():
                    for fn in sorted(os.listdir(d)):
                        if fn.startswith("mesowatch_") and fn.endswith(".json"):
                            self.import_file(os.path.join(d, fn))
            except Exception as e:  # noqa: BLE001
                dlog("MARKET-IMPORT-FAIL", type(e).__name__, e)
            time.sleep(120)

    def import_file(self, path):
        import shutil
        import sqlite3
        if time.time() - os.path.getmtime(path) < 5:
            return  # 아직 내려받는 중
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        ts = data.get("ts") or time.strftime("%Y-%m-%d %H:%M")
        cols = data["cols"]
        os.makedirs(MARKET_DIR, exist_ok=True)
        db = sqlite3.connect(MARKET_DB_PATH, timeout=10)
        try:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS items(item_id TEXT PRIMARY KEY, name_en TEXT, name_ko TEXT, category TEXT, tier TEXT, note TEXT);"
                "CREATE TABLE IF NOT EXISTS price_snapshots(ts TEXT, item_id TEXT, price REAL, p25 REAL, p75 REAL, asks INTEGER,"
                " sold INTEGER, ask_median REAL, source TEXT, PRIMARY KEY(ts,item_id));"
                "CREATE TABLE IF NOT EXISTS daily_median(date TEXT, item_id TEXT, median REAL, source TEXT, PRIMARY KEY(date,item_id));")
            n = 0
            for row in data["items"]:
                it = dict(zip(cols, row))
                name = it.get("name")
                if not name:
                    continue
                db.execute("INSERT OR IGNORE INTO items(item_id, name_en, category) VALUES(?,?,?)", (name, name, it.get("cat")))
                db.execute("INSERT OR IGNORE INTO price_snapshots VALUES(?,?,?,?,?,?,?,?,?)",
                           (ts, name, it.get("price"), it.get("p25"), it.get("p75"), it.get("asks"), it.get("sold"),
                            it.get("askMed"), "meso.watch via chrome"))
                for d, m in it.get("hist") or []:
                    if d and m and d < ts[:10]:  # 진행 중인 오늘 중앙값은 확정 전이라 제외
                        db.execute("INSERT OR IGNORE INTO daily_median VALUES(?,?,?,?)", (d, name, m, "meso.watch"))
                n += 1
            db.commit()
        finally:
            db.close()
        raw = os.path.join(MARKET_DIR, "raw")
        os.makedirs(raw, exist_ok=True)
        shutil.move(path, os.path.join(raw, os.path.basename(path)))
        dlog("MARKET-IMPORT", os.path.basename(path), n, "items")

ISSUE_PATH = os.path.join(APP_DIR, "문제기록.txt")
ISSUE_DIR = os.path.join(APP_DIR, "문제기록_이미지")
_issue_seen = set()
_EN_OK = {"INT", "DEX", "STR", "LUK", "HP", "MP", "AP", "SP", "PQ", "KPQ", "LPQ", "OPQ", "FM", "NX", "CH",
          "ATT", "MA", "GM", "AFK", "LOL", "OK", "PC", "DM", "VIP", "EXP", "NPC", "ID", "LV", "MESO"}


def report_issue(reason, raw="", orig="", tr="", img=None, rect=None):
    key = reason + "|" + normalize(raw or orig)[:60]
    if key in _issue_seen or len(_issue_seen) > 2000:
        return
    _issue_seen.add(key)
    try:
        if os.path.exists(ISSUE_PATH) and os.path.getsize(ISSUE_PATH) > 1_000_000:
            return
        pic = ""
        if img is not None and rect:
            os.makedirs(ISSUE_DIR, exist_ok=True)
            x, y, w, h = rect
            pic = time.strftime("%m%d_%H%M%S_") + str(len(_issue_seen)) + ".png"
            img.crop((0, max(0, y - 3), img.width, min(img.height, y + h + 3))).save(os.path.join(ISSUE_DIR, pic))
        with open(ISSUE_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%m-%d %H:%M:%S')}] {reason}\n  OCR: {raw}\n  원문: {orig}\n  번역: {tr}\n"
                    + (f"  이미지: {pic}\n" if pic else "") + "\n")
    except Exception:  # noqa: BLE001
        pass


def english_left(tr, orig):
    """번역 결과에 남은 영어 단어 (닉네임·스탯 약어 제외)."""
    words = re.findall(r"[A-Za-z][A-Za-z']{2,}", re.sub(r"\[[^\]]*\]", "", tr or ""))
    return [w for w in words if w.upper() not in _EN_OK]


def merge_item_links(msg, items):
    """영어 OCR이 깨뜨린 [아이템] 자리를 한국어 OCR로 읽은 [아이템 이름]으로 교체."""
    if not items:
        return msg
    for item in items:
        if item in msg:
            continue
        m = re.search(r"\[[^\[\]]*\]", msg)
        if m and not HANGUL_RE.search(m.group(0)):
            msg = msg[:m.start()] + item + msg[m.end():]
        else:
            msg = item + " " + msg
    return msg


# ----------------------------------------------------------------------------
# 채팅 종류 구분 (줄 배경색 / 글자색)
# ----------------------------------------------------------------------------
# 배경색이 있는 줄 = 확성기 종류. 기준색은 성윤님 스크린샷에서 직접 측정.
BG_KINDS = [
    ("확성기", (205, 155, 178), "#ff9ccf"),   # 분홍 배경
    ("고확",   (223, 174, 2),   "#ffc93c"),   # 노랑 배경 (아이템 확성기)
    ("특확",   (81, 207, 191),  "#5fe0cf"),   # 청록 배경
]
# 배경이 없는 줄 = 글자색으로 구분
TEXT_KINDS = [
    ("공지",   (255, 255, 0),   "#8f98ab"),   # 노란 글씨 = 게임 공지 (운영자 사칭 주의 등)
    ("파티",   (255, 153, 204), "#ff99cc"),
    ("귓속말", (0, 255, 0),     "#7CFC00"),
    ("길드",   (187, 136, 255), "#c39bff"),
    ("친구",   (255, 170, 0),   "#ffaa00"),
]
KIND_COLORS = {k: c for k, _, c in BG_KINDS + TEXT_KINDS}
KIND_COLORS["일반"] = "#d0d6e2"

# 아이템 설명창(마우스 올렸을 때 뜨는 창)이 채팅창 위에 겹쳐서 읽힌 줄
_TOOLTIP_RE = re.compile(r"^\s*(max\s?hp|max\s?mp|hp|mp|str|dex|int|luk|weapon\s?(att|attack|def)|magic\s?(att|attack|def)|"
                         r"w\.?\s?def|m\.?\s?def|accuracy|avoid\w*|speed|jump|req\s?\w+|upgrade\w*|분류|업그레이드)"
                         r"\.?\s*[:：.]?\s*[+-]?\d+(\s*\(\d+\s*[+-]\s*\d+\))?\s*$", re.I)  # '이름 : +숫자' 만 있는 줄
_NOTICE_RE = re.compile(r"[\[(]\{?공지|공지[\]」)]|초대\s*허|게임\s*옵|개인정보|사기\s*행위|주의하시기|알려드립니다|운영자|obtained\s.{2,50}\sfrom\s(the\s)?|congratulations!?$|congrats!?$"
                        # 파티 시스템 메시지 (들어옴/나감/추방/해체/파티장 변경/초대)
                        r"|파티를\s*(탈퇴|떠|해체|결성|만들)|파티에\s*(가입|참여|들어|초대)|파티장|파티에서\s*(추방|강퇴|탈퇴)|초대(를|했|하였)"
                        r"|(has\s+)?(left|joined)\s+(the\s+)?party|been\s+(expelled|kicked)|party\s+(has\s+been\s+)?disbanded"
                        r"|(is\s+now|became)\s+the\s+party\s+leader|invited\s+you|party\s+invitation|already\s+in\s+a\s+party", re.I)


# ---- 거래 글 판별 / 찾는 물건(찜) ------------------------------------------
_SELL_RE = re.compile(r"(?<![A-Za-z])S\s?>|\bwts\b|\bsell(?:ing)?\b|\bvendo\b|\bvendiendo\b|\bbenta\b"
                      r"|(?<![A-Za-z])a\s?/\s?w\b|(?<![A-Za-z])c\s?/\s?[o0]\b|\bobo\b|\bpm[o0]\b|\bpm offers?\b"
                      r"|\[팝니다\]", re.I)
_BUY_RE = re.compile(r"(?<![A-Za-z])B\s?>|\bwtb\b|\bbuy(?:ing)?\b|\bcompro\b|\bbibili\b|\[삽니다\]", re.I)
_MONEY_RE = re.compile(r"(?<![A-Za-z0-9+.])(\d+(?:[.,]\d+)?)\s*(k|kk|m|mil|mill|million)(?![A-Za-z]|\.\s?(?:def|att|atk|acc|avo|matk|ma\b))", re.I)
_MONEY_KO_RE = re.compile(r"(\d+(?:\.\d+)?)\s*억(?:(\d+)만)?|(\d+(?:\.\d+)?)\s*만(?:(\d+)천)?(?:(\d)백)?|(?<![\d만])(\d+)천(?:(\d)백)?")


# 찜 키워드(한국어) -> 같이 찾을 영어/줄임말
WATCH_ALIASES = {
    "귀고리지력": ["earringint", "earint", "귀지력", "귀고리지력"], "귀지력": ["earringint", "귀고리지력"],
    "귀고리민첩": ["earringdex", "귀덱"], "귀고리힘": ["earringstr"], "귀고리운": ["earringluk"],
    "장갑공격력": ["gloveatt", "glovesatt", "장공"], "장공": ["gloveatt", "glovesatt", "장갑공격력"],
    "장갑마력": ["glovema", "glovesma", "장마"], "장마": ["glovema", "장갑마력"],
    "완드마력": ["wandma", "wandmatk", "완마"], "완마": ["wandma", "완드마력"],
    "스태프마력": ["staffma", "staffmatk", "staffmatt", "스마"], "스마": ["staffma", "스태프마력"],
    "전신지력": ["overallint", "전지"], "전신민첩": ["overalldex", "전민"],
    "아대공격력": ["clawatt", "clawattack", "아대공"], "아대공": ["clawatt", "아대공격력"],
    "망토지력": ["capeint"], "망토민첩": ["capedex"],
    "투구명중": ["helmacc", "hatacc"], "신발이동속도": ["shoespeed", "shoesspeed"],
    "페어리완드": ["fairywand"], "이블윙즈": ["evilwings"], "크로미": ["cromi"],
    "에메랄드귀고리": ["emeraldearring"], "목장갑": ["workgloves"],
}


_SELL_STRONG = re.compile(r"(?<![A-Za-z])S\s?>|\bwts\b|\[팝니다\]", re.I)
_BUY_STRONG = re.compile(r"(?<![A-Za-z])B\s?>|\bwtb\b|\[삽니다\]", re.I)


def trade_of(msg, strong=False):
    """'sell' (팝니다) / 'buy' (삽니다) / None. 한 줄에 둘 다 있으면 먼저 나온 쪽.
    strong=True 면 S> B> 같은 확실한 표시만 봄."""
    if strong:
        s_m, b_m = _SELL_STRONG.search(msg), _BUY_STRONG.search(msg)
    else:
        s_m, b_m = _SELL_RE.search(msg), _BUY_RE.search(msg)
    if s_m and b_m:
        return "sell" if s_m.start() < b_m.start() else "buy"
    return "sell" if s_m else "buy" if b_m else None


def prices_in(text):
    """글 안의 메소 금액들 (메소 단위 정수)."""
    out = []
    for m in _MONEY_RE.finditer(text):
        num = float(m.group(1).replace(",", "."))
        out.append(int(round(num * (1_000 if m.group(2).lower().startswith("k") else 1_000_000))))
    def exact(m):  # format_meso 의 '6만4990' / '1550메소' 표기
        out.append(int(m.group(1) or 0) * 10_000 + int(m.group(2) or m.group(3)))
        return " "
    text = re.sub(r"(?<![\d.])(?:(\d+)만(\d{4})|(\d{4,5})메소)(?!\d)", exact, text)
    for m in _MONEY_KO_RE.finditer(text):
        if m.group(1):
            out.append(int(float(m.group(1)) * 100_000_000 + int(m.group(2) or 0) * 10_000))
        elif m.group(3):
            out.append(int(float(m.group(3)) * 10_000 + int(m.group(4) or 0) * 1_000 + int(m.group(5) or 0) * 100))
        else:
            out.append(int(m.group(6)) * 1_000 + int(m.group(7) or 0) * 100)
    return [p for p in out if p >= 1000]


def ai_output_ok(orig, tr):
    """AI 번역을 써도 되는지: 가격(메소 금액)이 원문과 똑같을 때만."""
    if not tr or "(추정)" in tr:
        return False
    return set(prices_in(normalize_trade(orig))) == set(prices_in(tr))


def parse_price_input(text):
    """사용자 입력 '30' / '30만' / '300k' / '1.2m' -> 메소. 빈칸이면 None."""
    t = text.strip().replace(",", "")
    if not t:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", t):
        return int(float(t) * 10_000)  # 숫자만 쓰면 '만' 단위
    p = prices_in(t)
    return p[0] if p else None


def _squash(t):
    return re.sub(r"\s+", "", (t or "").lower())


def watch_match(watches, texts, trade):
    """찜 목록과 비교. 맞으면 (찜 항목, 가격 설명) 반환, 아니면 None. 판매 글만."""
    if trade != "sell" or not watches:
        return None
    blob = _squash(" ".join(t for t in texts if t))
    for w in watches:
        words = [_squash(x) for x in re.split(r"[\s,]+", w.get("keyword", "")) if x.strip()]
        alts = [_squash(a) for a in w.get("aliases", [])]
        if not words:
            continue
        if not (all(x in blob for x in words) or any(a and a in blob for a in alts)):
            continue
        limit = w.get("max_price")
        prices = prices_in(" ".join(t for t in texts if t))
        if limit:
            if prices and min(prices) > limit:
                continue
            note = f"{format_meso(min(prices))}" if prices else "가격 미표시"
        else:
            note = format_meso(min(prices)) if prices else ""
        return w, note
    return None


# 보기 탭: 이름 -> 보여줄 종류 (None = 전부)
VIEW_TABS = {
    "전체": None,
    "대화": {"일반", "파티", "귓속말", "길드", "친구"},
    "일반": {"일반"},
    "파티": {"파티"},
    "고확": {"고확"},
    "특확": {"특확"},
    "확성기": {"확성기", "고확", "특확"},
    "귓속말": {"귓속말"},
    "팝니다": "sell",
    "삽니다": "buy",
    "★찜": "watch",
}


def _dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def _chroma(c):
    t = float(sum(c)) or 1.0
    return (c[0] / t, c[1] / t, c[2] / t)


def classify_line(img, rect):
    """채팅 한 줄의 종류를 배경색/글자색으로 판별."""
    if img is None or rect is None:
        return "일반"
    x, y, w, h = rect
    x0, y0 = max(0, x - 2), max(0, y)
    x1, y1 = min(img.width, x + w + 2), min(img.height, y + h)
    if x1 - x0 < 4 or y1 - y0 < 3:
        return "일반"
    pixels = list(img.crop((x0, y0, x1, y1)).getdata())[::2]
    n = len(pixels) or 1
    # 1) 배경색: 확성기 줄은 배경이 한 색으로 칠해져 있음.
    #    밝기와 상관없이 '색 비율'(chroma)이 확성기 색과 같은 픽셀이 줄의 30% 이상이면 확성기
    #    (채팅창 투명도에 따라 확성기 색이 밝게/어둡게 보여도 구분, 얇은 파티 글자는 30%가 안 됨)
    refs = [(kind, _chroma(ref)) for kind, ref, _c in BG_KINDS]
    hits = {kind: 0 for kind, _r in refs}
    for p in pixels:
        if sum(p) <= 150 or max(p) - min(p) <= 25:
            continue
        pc = _chroma(p)
        for kind, rc in refs:
            if _dist(pc, rc) < 0.05:
                hits[kind] += 1
                break
    best = max(hits.items(), key=lambda kv: kv[1])
    if best[1] / n >= 0.30:
        return best[0]
    # 2) 글자색 (밝은 픽셀 중 가장 많은 색)
    bright = [p for p in pixels if max(p) > 150]
    if len(bright) < 5:
        return "일반"
    counts = {kind: sum(1 for p in bright if _dist(p, ref) < 60) for kind, ref, _c in TEXT_KINDS}
    # 색 계열로도 한 번 더 셈 (글자 가장자리가 흐려져 기준색과 멀어지는 경우 대비)
    counts["파티"] += sum(1 for p in bright if p[0] > 190 and p[0] - p[1] > 45 and p[2] - p[1] > 15)
    counts["귓속말"] += sum(1 for p in bright if p[1] > 170 and p[1] - p[0] > 70 and p[1] - p[2] > 70)
    counts["길드"] += sum(1 for p in bright if p[2] > 190 and p[2] - p[1] > 60 and p[0] - p[1] > 20 and p[0] < 220)
    counts["친구"] += sum(1 for p in bright if p[0] > 220 and 120 < p[1] < 200 and p[2] < 60)
    counts["공지"] += sum(1 for p in bright if p[0] > 220 and p[1] > 220 and p[2] < 70)
    kind, cnt = max(counts.items(), key=lambda kv: kv[1])
    white = sum(1 for p in bright if min(p) > 200)
    if DEBUG[0]:
        avg = tuple(sum(c[i] for c in bright) // len(bright) for i in range(3))
        dlog("COLOR", rect, "avg", avg, "white", white, counts)
    if cnt >= 5 and cnt >= white * 0.6:
        return kind
    return "일반"


# ----------------------------------------------------------------------------
# AI 번역 (Claude) - 진짜 한국 게이머 말투로 다듬기
# ----------------------------------------------------------------------------
AI_PROMPT = """너는 메이플스토리 클래식 월드(구버전 메이플, GMS) 채팅을 한국 유저에게 실시간으로 통역해 주는 통역사야.
입력은 "번호|채팅" 형식의 줄들이고, 각 줄은 다른 유저가 친 채팅 한 줄이야(영어, 스페인어 등).
출력도 똑같이 "번호|번역" 형식으로, 입력과 같은 번호를 빠짐없이 한 줄씩만 써. 설명, 인사, 코드블록 등 다른 글자는 절대 쓰지 마.

번역 규칙:
- 한국 메이플 유저가 실제로 채팅에서 쓰는 자연스러운 말투로. 직역 금지, 짧고 구어체로.
- 거래 표기는 반드시 한국어로 바꿔서 맨 앞에 써: S> = [팝니다], B> = [삽니다], T> = [교환], L> = [구해요], F> = [찾아요], WTS = [팝니다], WTB = [삽니다], c/o = 현재 최고가, a/w = 즉시 낙찰가, obo/o.o = 가격 제안 가능, ea = 개당, clean = 노작, 0 slot = 업횟 0.
- 금액: k = 천, m = 백만. 한국식 만 단위로 바꿔. 예) 500k -> 50만, 1.2m -> 120만, 175k -> 17만5천.
- 주문서: MA = 마력, att = 공격력, 60% = 60%, 귀 지력 60%(earring INT 60%), 장공 60%(glove att 60%), 장마(glove MA), 완마/스마(wand/staff MA), 전민/전지/전힘/전운(overall DEX/INT/STR/LUK).
- 줄임말: gz = 축하해, ty = 고마워, tyfp = 파티 고마워, brb = 금방 올게, gtg = 가봐야겠다, afk = 잠수, ks = 겹사, kpq = 커파, pq = 파퀘, fm = 자유시장, ch = 채널, cc = 채널 이동, pots = 물약, leech = 쩔, imo = 내 생각엔, lol/lmao = ㅋㅋ.
- 직업: sin = 어쌔신, dit/bandit = 시프, I/L = 썬콜, F/P = 불독, cleric = 클레릭, fighter = 파이터, page = 페이지, spearman = 스피어맨, hunter = 헌터, crossbowman = 사수 (단, xbow 60% 처럼 주문서·무기 얘기면 xbow = 석궁).
- 아이템 이름은 한국 메이플 공식 이름을 알면 그 이름으로, 모르면 영어 그대로 둬.
- [대괄호] 아이템 링크: 대괄호는 유지하고, 안의 한글은 한 글자도 바꾸지 말고 그대로, 영어 부분만 한국 메이플 공식 이름으로 번역해.
  예) [Emerald Earrings] → [에메랄드 귀고리], [장갑 Attack 주문서] → [장갑 공격력 주문서], [에메랄드 귀고리] → [에메랄드 귀고리]
- 닉네임은 번역하지 마.
- 닉네임, 숫자, % , +2 같은 강화 수치는 바꾸지 마.
- 오타·OCR 깨짐(예: 0과 O 혼동)은 문맥으로 바로잡아서 번역해.
- 나라별 게이머 은어도 한국 게이머 말로 바꿔:
  영어: ngl=솔직히, fr=ㄹㅇ, bro/bruh=형/야, sus=수상한데, ez=개쉽네, rip=망했다, gg=ㅈㅈ/수고, y'all=다들, spam=도배, scam/scammer=사기/사기꾼, report=신고, hacker/bot=핵쟁이/매크로, noob=뉴비, rich=부자, broke=거지, pls/plz=좀, fame=인기도, defame=인기도 내리기, "x for fame"/"fame for fame"=맞인기(인기도 서로 올려주기) 하자, f4f=맞인기, tyfp=파티 고마워.
  스페인어: jaja/jajaja=ㅋㅋㅋ, xd=ㅋㅋ, buenas=ㅎㅇ/안녕, que onda=뭐해/ㅎㅇ, wey/güey/bro=야/형, pq=왜, q=뭐, xq=왜, tmb=나도, vendo=팝니다, compro=삽니다, alguien=누구, cuanto=얼마.
  포르투갈어(브라질): kkkk=ㅋㅋㅋ, vc=너, mn=야, tmj=ㄱㅅ/파이팅, vendo/compro=팝니다/삽니다, pq=왜.
  필리핀(타갈로그): po=요, pre/pare=형, tara=가자, sige=ㅇㅋ, salamat=고마워.
  베트남어: ae=형들/다들, dc=됨, ko/k=안 돼/아니, mn=다들.
- OCR이 글자를 깨뜨린 경우(예: "thats mor thần a." → "that's more than a ...")는 원래 문장을 추측해서 자연스럽게 번역해. (추정) 같은 표시나 "~의 오타로 보여서" 같은 설명은 절대 붙이지 말고 번역만 써. 도저히 알 수 없는 찌꺼기면 "번역 불가"라고만 써.
- 일상 대화(잡담·농담·드립·감정 표현)는 메이플 하는 한국 친구가 단톡방에서 말하듯 자연스럽게 의역해:
  · 말투 세기를 그대로 살려: 장난이면 장난스럽게(ㅋㅋ, ㅎㅎ), 화내면 세게, 비꼬면 비꼬게, 정중하면 존댓말. 기본은 반말 게이머 말투.
  · 직역하면 어색한 관용구·밈은 한국식 표현으로: "that's crazy"=미쳤다/ㄷㄷ, "no way"=에바/말도 안 돼, "my bad"/"mb"=내 탓/미안, "same"=ㄹㅇ 나도, "fair"=인정, "W"=개이득/굿, "L"=망했네/졌네, "ratio"=반박 ㅈㅈ, "touch grass"=밖에 좀 나가라, "skill issue"=실력 문제, "cope"=정신승리, "based"=소신 발언 ㄹㅇ, "cringe"=오글거려, "lowkey"=은근, "deadass"/"fr fr"/"no cap"=ㄹㅇ 진짜, "cap"(거짓말 의미일 때)=구라, "bet"=ㅇㅋ 콜, "slay"=찢었다, "goated"/"the goat"/"GOAT"=개쩐다/최고, "mid"(평가 의미일 때)=그저 그래, "sus"=수상한데, "rizz"=작업멘트, "ong"=진짜로, "idc"=상관없어, "ikr"=그니까, "smh"=어이없네, "wyd"=뭐해, "hbu/wbu"=너는?, "irl"=현실에서, "lmk"=알려줘, "ttyl"=나중에 얘기해, "bbl"=이따 올게, "gn"=잘 자, "ily"=사랑해(친구끼리 장난), "omg"=헐, "wtf"=뭐야 이게/뭔데, "ffs"=아 진짜, "istg"=진짜 맹세함, "idek"=나도 모르겠다, "mfw"=그때 내 표정, "kek"=ㅋㅋ.
  · 게이머 표현: "repot"/"repotting"=물약 사러 감, "scalper(s)"=되팔이, "bot check"=매크로 검사, "W me"/"whisp me"=귓 주세요, "mega"/"smega"=확성기, "last smega"=마지막 확성기, "min"(스탯 뒤)=이상, "x left"=x개 남음, "for sale"=팝니다, "first come"/"fcfs"=선착순.
  · 주문서·강화 얘기: "60s"/"10s"/"30s"(주문서 얘기일 때)=60%/10%/30% 주문서, "bricked"/"boomed"=작 다 실패해서 템 망함(터짐), "skem"/"scam"=사기, "s/b"=시작가, "buy now"(파는 사람 문구)=지금 바로 사세요, "take this off my hands"/"take dis off my handz"=떨이로 넘겨요(빨리 처분), "poached"=자리 뺏김, "we're full"/"been full"=파티 꽉 찼어, "wsp"/"whisp"=귓 주세요.
  · 사람 이름·지명·스포츠 팀(Heat 등)·게임 밖 고유명사는 그대로 두고 나머지만 번역.
  · 질문은 질문으로(…?), 감탄은 감탄으로. 문장 끝 이모티콘(:D, :), xD, <3)은 ㅋㅋ/ㅎㅎ/♥ 정도로 바꾸거나 그대로.
- 줄 끝에 ⟪용어 적용: ...⟫ 가 있으면, 그건 메이플 용어 사전으로 미리 바꿔둔 문장이야. 거기 나온 한국어 용어(노작, 크뎀, 장공, 귀지력, 목장갑, 17만5천 같은 금액, 25채널, 2개 등)는 바꾸지 말고 그대로 써서 자연스러운 문장으로 다듬어. 출력에는 ⟪⟫ 부분을 쓰지 마.
- 원문에 S>/B>/WTS/WTB/selling/buying 같은 표시가 없으면 [팝니다]/[삽니다]를 새로 붙이지 마.
- 거래 줄임말은 절대 영어로 남기지 마: pmo/PMO = "가격 제시 귓 주세요", obo/ono = "가격 제시 받아요", pm = "귓 주세요", c/o = 현재 최고가, a/w = 즉시 낙찰가.
- OCR이 철자를 깨뜨린 단어는 메이플 거래 문맥으로 원래 단어를 추측해서 번역해 (확실할 때만).
  예) "parrina"/"earrina"/"earing" → earring(귀고리), "f 7."/"6O%"/"6o%" 가 주문서 자리에 있으면 60%, "thesp" → these, "lnt"/"1nt" → INT
  예) "thesp + 3 parrina f 7. Int for 400k, PM" → [팝니다] 이거 +3 귀지력 60% 3개 40만, 귓 주세요
- OCR이 자주 틀리는 것: "AO"/"Alt"/"AH" 뒤에 장비 이름이 붙어 있으면 대부분 "Att"(공격력), "0"↔"O", "1"↔"l"↔"I". 예) "S>88 AORising" = "S>88 Att Rising" → [팝니다] 공 88 라이징
- pmo = 귓으로 가격 제안 주세요, pm = 귓 주세요, @ = 가격(예: @150k = 15만에).
- sin은 어쌔신(도적 직업)이지 '죄'나 썬콜이 아니야. 장공 60%처럼 이미 뜻이 들어간 말 뒤에 '공격력'을 또 붙이지 마.
- 의미를 도저히 모르겠으면 원문을 그대로 넣어.

예시 입력:
1|S>Red Viper +2 S>Niam +4 B>Kumbi 290k
2|did this sin down here get lie detector?
3|WTS +14 69 atk Dankke 500k obo, B>Glove att 60%
4|INKWELL HEY BUFF BANDIT PLEASE
5|gz on 2nd job!! tyfp
6|L>KPQ need 1 more, 3/4
예시 출력:
1|[팝니다] 레드 바이퍼 +2, 니암 +4 / [삽니다] 금비 표창 29만
2|여기 있던 어쌔신 거짓말 탐지기 걸렸어?
3|[팝니다] +14 공 69 단케 50만 (가격 제안 가능) / [삽니다] 장공 60%
4|INKWELL님 시프 버프 좀 주세요
5|2차 전직 축하해!! 파티 고마워
6|[구해요] 커파 1명, 3/4"""


TRADE_FIX = [
    (re.compile(r"(?<![A-Za-z])S\s*>\s*", re.I), "[팝니다] "),
    (re.compile(r"(?<![A-Za-z])B\s*>\s*", re.I), "[삽니다] "),
    (re.compile(r"(?<![A-Za-z])T\s*>\s*", re.I), "[교환] "),
    (re.compile(r"(?<![A-Za-z])L\s*>\s*", re.I), "[구해요] "),
    (re.compile(r"\bWTS\b", re.I), "[팝니다]"),
    (re.compile(r"\bWTB\b", re.I), "[삽니다]"),
    # AI가 영어 그대로 남긴 가격 관련 줄임말
    (re.compile(r"(?<![A-Za-z])p\.?\s?m\.?\s?[o0]\.?(?:'?s)?(?![A-Za-z])", re.I), "가격 제시 귓 주세요"),
    (re.compile(r"(?<![A-Za-z])o\.?b\.?o\.?(?![A-Za-z])", re.I), "가격 제시 받아요"),
    (re.compile(r"(?<![A-Za-z])c/o(?![A-Za-z])", re.I), "현재 최고가"),
    (re.compile(r"(?<![A-Za-z])a/w(?![A-Za-z])", re.I), "즉시 낙찰가"),
    (re.compile(r"(?<![A-Za-z])pm\s?me(?![A-Za-z])", re.I), "귓 주세요"),
]


def polish_ai(text):
    """AI가 놓친 거래 표기를 마저 한국어로 바꾸고 중복 표현 정리."""
    # AI가 ⟪용어 적용⟫ 안내문이나 "오타로 보여서 ~" 같은 설명을 같이 써 버린 경우 정리
    text = re.sub(r"\s*\(?⟪\s*용어\s*적용[^⟫]*⟫.*$", "", text)
    text = re.sub(r"⟪[^⟫]*⟫|[⟪⟫]", " ", text)
    if re.search(r"오타로 보|추정됩니다|번역합니다|로 보입니다|부분 제거|그대로 쓰면", text):
        return ""  # 설명문 -> 버리고 구글 번역 유지
    text = apply_ko_terms(text)
    for pat, rep in TRADE_FIX:
        text = pat.sub(rep, text)
    text = re.sub(r"(\[(팝니다|삽니다)\])([^\[]*?)\s*\2(?=\s|$|/|,)", r"\1\3", text)
    text = re.sub(r"장공\s*공격력", "장공", text)
    text = re.sub(r"장마\s*마력", "장마", text)
    return re.sub(r"\s{2,}", " ", text).strip()


OUT_PROMPT = """너는 메이플스토리 클래식 월드(GMS, 해외 서버) 채팅 통역사야.
한국 유저가 쓴 한국어 채팅을 {lang} 게임 채팅으로 바꿔줘.
규칙:
- 실제 해외 유저들이 게임에서 치는 짧고 자연스러운 채팅 말투로. 교과서 문장 금지.
- 거래: 팝니다=S>, 삽니다=B>, 교환=T>, 구해요=L>, 가격 제안 받아요=obo / taking offers, 귓 주세요=pm me, 현재 최고가=c/o, 즉시 낙찰가=a/w, 개당=ea.
- 금액: 한국식 만 단위를 k/m로. 17만5천 -> 175k, 120만 -> 1.2m, 50만 -> 500k.
- 메이플 용어: 장공=glove att, 장마=glove MA, 완마=wand MA, 스마=staff MA, 귀지력/귀 인트=earring INT, 귀덱=earring DEX, 전민/전지/전힘/전운=overall DEX/INT/STR/LUK, 노작=clean, 업횟=slots, 커파=KPQ, 파퀘=PQ, 자유시장=FM, 채널=ch, 겹사=KS, 쩔=leech, 물약=pots, 맞인기=fame for fame, 썬콜=I/L, 불독=F/P, 어쌔신=sin, 시프=bandit, 사수=xbow, 투구/모자=hat.
- 아이템 이름은 GMS 공식 영어 이름으로 (알면). 닉네임, 숫자, %, [대괄호] 내용은 그대로.
- 고마워=ty, 축하해=gz, 잠깐만/금방 올게=brb, ㅋㅋ=lol 처럼 게임 줄임말 적극 사용.
- 영어가 아닌 언어로 요청되면 그 나라 게이머 말투로.
- 원문에 없는 내용(obo, 가격, 인사 등)을 덧붙이지 마. 뜻은 정확히 그대로.
- 결과 채팅 한 줄만 출력해. 따옴표, 설명, 다른 글자 금지."""

LANG_CHOICES = {"영어": ("en", "영어(English)"), "스페인어": ("es", "스페인어(Español)"),
                "포르투갈어": ("pt", "브라질 포르투갈어(Português)"), "타갈로그어": ("tl", "필리핀 타갈로그어/영어 섞인 말투"),
                "베트남어": ("vi", "베트남어(Tiếng Việt)")}


class AITranslator:
    """config의 ai_engine:
       "claude_cli" = PC에 설치된 Claude Code(claude 명령어)로 번역 (구독 사용, 키 불필요)
       "api"        = Anthropic API 키로 번역 (빠름, 소액 과금)
       "off"        = 사용 안 함 (구글 번역만)"""

    def __init__(self, cfg):
        self.engine = (cfg.get("ai_engine") or "off").lower()
        self.cli_model = cfg.get("ai_cli_model") or "sonnet"
        self.api_key = (cfg.get("anthropic_api_key") or "").strip()
        self.api_model = cfg.get("ai_api_model") or "claude-haiku-5-5"
        self.cli_path = None
        self.error = ""
        if self.engine == "claude_cli":
            import shutil
            self.cli_path = shutil.which("claude") or shutil.which("claude.cmd") or shutil.which("claude.exe")
            if not self.cli_path:  # 방금 설치해서 PATH에 아직 안 잡힌 경우
                for cand in (os.path.expanduser(r"~\.local\bin\claude.exe"),
                             os.path.expandvars(r"%APPDATA%\npm\claude.cmd")):
                    if os.path.exists(cand):
                        self.cli_path = cand
                        break
            if not self.cli_path:
                self.error = "Claude Code(claude 명령어)를 못 찾았어요"
                self.engine = "off"
        elif self.engine == "api" and not self.api_key:
            self.error = "anthropic_api_key 가 비어 있어요"
            self.engine = "off"

    @property
    def enabled(self):
        return self.engine in ("claude_cli", "api")

    @staticmethod
    def _parse(text, n):
        out = {}
        for line in text.splitlines():
            m = re.match(r"^\s*(\d+)\s*[|｜]\s*(.*)$", line)
            if m:
                out[int(m.group(1))] = m.group(2).strip()
        if not out:
            raise ValueError("AI 응답 형식 오류")
        return [polish_ai(out[i + 1]) if out.get(i + 1) else None for i in range(n)]

    _hints = None

    @classmethod
    def glossary_hints(cls, text):
        """이번 채팅에 나온 용어만 골라서 AI에게 '공식 한국어 이름'으로 알려줌."""
        if cls._hints is None:
            try:
                with open(GLOSSARY_PATH, "r", encoding="utf-8") as f:
                    d = json.load(f)
                h = {}
                for sec in ("terms", "names"):
                    for k, v in (d.get(sec) or {}).items():
                        if not k.startswith("_") and len(k) >= 4:
                            h[k.lower()] = v
                cls._hints = h
            except Exception:  # noqa: BLE001
                cls._hints = {}
        low = text.lower()
        return [f"{k} = {v}" for k, v in cls._hints.items() if k in low][:40]

    def _hint_block(self, payload):
        hints = self.glossary_hints(payload)
        return ("\n\n이번 채팅에 나온 용어의 한국 메이플 공식 이름 (꼭 이대로 써):\n" + "\n".join(hints)) if hints else ""

    calls = 0
    lean_ok = [True]

    def run_cli(self, prompt):
        """claude CLI 실행. 가능하면 Claude Code 기본 지시문·도구를 빼고 가볍게 실행해서 토큰 절약."""
        import subprocess
        flags = 0x08000000 if os.name == "nt" else 0  # 콘솔 창 안 띄우기
        base = [self.cli_path, "-p", "--model", self.cli_model]
        lean = ["--system-prompt", "너는 번역 도구야. 사용자 지시에 적힌 형식대로 결과만 출력해.",
                "--tools", "", "--strict-mcp-config", "--setting-sources", ""]
        tries = [base + lean, base] if self.lean_ok[0] else [base]
        for args in tries:
            proc = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=90, creationflags=flags)
            if proc.returncode == 0 and proc.stdout.strip():
                AITranslator.calls += 1
                return proc.stdout
            if args is not tries[-1]:
                dlog("AI-LEAN-FAIL", proc.returncode, (proc.stderr or proc.stdout)[:200])
                self.lean_ok[0] = False  # 이 버전 claude 는 가벼운 옵션을 모름 -> 기본 실행
        dlog("AI-ERR", proc.returncode, proc.stderr[:300], proc.stdout[:300])
        raise RuntimeError((proc.stderr or proc.stdout or "claude 실행 실패").strip()[:120])

    def translate_batch(self, msgs):
        payload = "\n".join(f"{i + 1}|{m.replace(chr(10), ' ')}" for i, m in enumerate(msgs))
        if self.engine == "claude_cli":
            out = self.run_cli(AI_PROMPT + self._hint_block(payload) + "\n\n이번 입력:\n" + payload)
            dlog("AI-IN", payload.replace("\n", " / "))
            dlog("AI-OUT", out.strip().replace("\n", " / "))
            return self._parse(out, len(msgs))
        if self.engine == "api":
            import requests
            r = requests.post(
                "https://api.anthropic.com/v1/messages", timeout=30,
                headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": self.api_model, "max_tokens": 2000, "system": AI_PROMPT + self._hint_block(payload),
                      "messages": [{"role": "user", "content": payload}]})
            if r.status_code != 200:
                raise RuntimeError(f"API {r.status_code}: {r.text[:120]}")
            text = "".join(b.get("text", "") for b in r.json().get("content", []))
            return self._parse(text, len(msgs))
        raise RuntimeError("AI 번역 꺼짐")


# ----------------------------------------------------------------------------
# 처리 파이프라인 (OCR 줄 -> 번역 결과)
# ----------------------------------------------------------------------------
def quick_translate(text, sl="ko", tl="en"):
    """구글 빠른 번역 (내 채팅 보내기용 미리보기)."""
    import requests
    r = requests.get("https://translate.googleapis.com/translate_a/single", timeout=6,
                     params={"client": "gtx", "sl": sl, "tl": tl, "dt": "t", "q": text},
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return "".join(seg[0] for seg in r.json()[0] if seg and seg[0])


def ai_outbound(ai, text, lang_label):
    """한국어 -> 게임 채팅 말투 외국어 (Claude)."""
    prompt = OUT_PROMPT.format(lang=lang_label) + "\n\n번역할 한국어 채팅:\n" + text
    if ai.engine == "claude_cli":
        out = ai.run_cli(prompt).strip()
    elif ai.engine == "api":
        import requests
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=30,
                          headers={"x-api-key": ai.api_key, "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"},
                          json={"model": ai.api_model, "max_tokens": 300,
                                "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        out = "".join(b.get("text", "") for b in r.json().get("content", [])).strip()
    else:
        raise RuntimeError("AI 꺼짐")
    out = out.splitlines()[-1].strip().strip('"').strip("'") if out else ""
    dlog("OUT-AI", text, "=>", out)
    return out


def translate_message(msg, glossary, translator):
    protected, slots = glossary.protect(msg)
    translated = translator.translate(protected)
    return glossary.restore(translated, slots)


# ----------------------------------------------------------------------------
# 자동 업데이트 (GitHub)
# ----------------------------------------------------------------------------
def auto_update():
    """GitHub에 새 버전이 있으면 파일을 받아 교체하고 True 반환."""
    try:
        import requests
        from urllib.parse import quote
        r = requests.get(UPDATE_BASE + "version.json", timeout=5,
                         params={"t": int(time.time())})
        r.raise_for_status()
        info = r.json()
        remote = int(info.get("version", 0))
        if remote <= APP_VERSION:
            print(f"최신 버전입니다 (v{APP_VERSION})")
            return False
        print(f"새 버전 v{remote} 발견 -> 업데이트 중... ({info.get('note', '')})")
        downloaded = {}
        for name in info.get("files", []):
            fr = requests.get(UPDATE_BASE + quote(name), timeout=15,
                              params={"t": int(time.time())})
            fr.raise_for_status()
            downloaded[name] = fr.content
        for name, data in downloaded.items():  # 전부 받은 뒤에 한꺼번에 교체
            target = os.path.join(APP_DIR, name)
            tmp = target + ".new"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, target)
        print(f"업데이트 완료! v{APP_VERSION} -> v{remote}")
        return True
    except Exception as e:  # noqa: BLE001
        print("업데이트 확인 실패 (그냥 실행합니다):", e)
        return False


# ----------------------------------------------------------------------------
# 화면 UI (tkinter)
# ----------------------------------------------------------------------------
def run_app():
    import tkinter as tk
    from tkinter import messagebox

    # 고해상도 모니터에서 좌표가 어긋나지 않게
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        pass

    cfg = load_config()
    DEBUG[0] = bool(cfg.get("debug"))
    glossary = Glossary()
    translator = Translator(cfg["target_lang"], cfg.get("deepl_api_key", ""))
    seen = SeenLines()
    memory = AIMemory()
    out_q = queue.Queue()
    ai = AITranslator(cfg)
    ai_q = queue.Queue()
    id_counter = [0]

    def next_id():
        id_counter[0] += 1
        return id_counter[0]
    state = {"running": False, "worker": None}

    try:
        ocr = ScreenOCR(cfg["ocr_lang"], cfg["upscale"])
    except Exception as e:  # noqa: BLE001
        ocr = None
        ocr_error = str(e)
    else:
        ocr_error = ""

    root = tk.Tk()
    root.title("메이플 채팅 번역기")
    root.geometry(cfg.get("overlay_geometry") or DEFAULT_CONFIG["overlay_geometry"])
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.9)
    root.configure(bg="#14161c")

    bar = tk.Frame(root, bg="#20232c")
    bar.pack(fill="x")
    status = tk.Label(bar, text="F9: 채팅창 영역 선택 → F8: 시작", fg="#9aa3b5",
                      bg="#20232c", font=("Malgun Gothic", 9))
    # 맨 왼쪽: 켜기/끄기 큰 버튼 (창이 좁아도 안 잘리게 제일 먼저 배치)
    power_btn = tk.Button(bar, text="▶ 켜기", bg="#2f9e44", fg="white", activebackground="#40c057",
                          activeforeground="white", bd=0, font=("Malgun Gothic", 10, "bold"),
                          padx=12, pady=2, cursor="hand2")
    power_btn.pack(side="left", padx=(6, 2), pady=3)
    status.pack(side="left", padx=6, pady=4)
    status.configure(cursor="hand2")
    status.bind("<Button-1>", lambda e: toggle())

    def btn(text, cmd):
        b = tk.Button(bar, text=text, command=cmd, bg="#2d3240", fg="#e8ecf4",
                      activebackground="#3b4255", activeforeground="white", bd=0,
                      font=("Malgun Gothic", 9), padx=8, pady=2)
        b.pack(side="right", padx=3, pady=3)
        return b

    tabs = tk.Frame(root, bg="#181b22")
    tabs.pack(fill="x")
    view_var = tk.StringVar(value=cfg.get("view_tab", "전체") if cfg.get("view_tab") in VIEW_TABS else "전체")
    tab_btns = {}

    def set_view(name):
        view_var.set(name)
        cfg["view_tab"] = name
        for n, b in tab_btns.items():
            on = n == name
            b.configure(bg="#3b6ef5" if on else "#2d3240", fg="white" if on else "#c9cfdb")
        render()

    for _name in VIEW_TABS:
        _b = tk.Button(tabs, text=_name, command=lambda n=_name: set_view(n), bd=0,
                       bg="#2d3240", fg="#c9cfdb", activebackground="#5a86ff", activeforeground="white",
                       font=("Malgun Gothic", 9), padx=7, pady=1)
        _b.pack(side="left", padx=2, pady=3)
        tab_btns[_name] = _b

    # --- 경험치 측정 바 (타이머 + 먹은 경험치 + 시간당 경험치, 사냥터별 기록) -------------
    from tkinter import ttk as _ttk
    exp = ExpTracker()
    exp_state = {"thread": None, "raw": "", "last_log": 0.0, "seg_map": None, "seg_party": None,
                 "ocr": None, "shown": False}
    exp_bar = tk.Frame(root, bg="#1b2430")
    exp_lbl = tk.Label(exp_bar, text="", fg="#ffd43b", bg="#1b2430", anchor="w", justify="left",
                       font=("Malgun Gothic", 10, "bold"))
    exp_row = tk.Frame(exp_bar, bg="#1b2430")
    exp_row.pack(side="right", padx=4, pady=3)
    exp_lbl.pack(side="left", fill="x", expand=True, padx=8, pady=3)
    exp_map = tk.StringVar(value="")      # (예전 사냥터/파티 칸 - 화면에서는 뺐음)
    exp_party = tk.StringVar(value="")
    def ebtn(text, cmd, color="#2d3240"):
        b = tk.Button(exp_row, text=text, command=cmd, bg=color, fg="white", activebackground="#3b4255",
                      activeforeground="white", bd=0, font=("Malgun Gothic", 9), padx=7, pady=1)
        b.pack(side="right", padx=2)
        return b

    def exp_write(kind):
        """exp_log.csv 에 한 줄 기록 (나중에 사냥터별 효율 비교용)."""
        t = exp.total_time()
        if t < 60 and kind != "중간":
            return
        try:
            new = not os.path.exists(EXP_LOG_PATH)
            with open(EXP_LOG_PATH, "a", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["기록시각", "종류", "사냥터", "파티인원", "경과초", "누적경험치", "누적퍼센트",
                                "시간당경험치", "시간당퍼센트", "레벨업", "현재경험치", "필요경험치"])
                last = exp.last or {}
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), kind, exp_state["seg_map"] or exp_map.get().strip(),
                            exp_state["seg_party"] or exp_party.get(), int(t), exp.gained_exp(), round(exp.gained_pct, 2),
                            int(exp.rate()), round(exp.pct_rate(), 2), exp.levelups,
                            (int(exp.need * last["pct"] / 100) if exp.need and last.get("pct") is not None else ""),
                            exp.need or ""])
        except Exception as e:  # noqa: BLE001
            dlog("EXP-LOG-ERR", repr(e))

    def exp_loop():
        while exp.running:
            t0 = time.time()
            try:
                raw, rd = exp_state["ocr"].read_exp(cfg["exp_region"])
                exp_state["raw"] = raw
                dlog("EXP", raw, rd)
                exp.feed(rd)
            except Exception as e:  # noqa: BLE001
                dlog("EXP-ERR", repr(e))
                exp.feed(None)
            time.sleep(max(0.5, float(cfg.get("exp_interval", 3)) - (time.time() - t0)))

    def exp_begin_segment():
        exp_state["seg_map"] = ""
        exp_state["seg_party"] = ""

    def exp_toggle():
        if exp.running:
            exp.pause()
            exp_write("정지")
            exp_go.configure(text="▶ 측정", bg="#2f9e44")
            exp_update()
            return
        if not cfg.get("exp_region"):
            info("먼저 경험치 숫자가 있는 곳을 드래그해서 지정하세요.")
            select_region("exp_region")
            return
        if exp_state["ocr"] is None:
            try:
                exp_state["ocr"] = ScreenOCR("en", 3.0)
            except Exception as e:  # noqa: BLE001
                info("OCR 준비 실패: " + str(e), "err")
                return
        if exp_state["seg_map"] is None:
            exp_begin_segment()
        exp.start()
        exp_state["last_log"] = time.time()
        exp_go.configure(text="■ 정지", bg="#c92a2a")
        exp_state["thread"] = threading.Thread(target=exp_loop, daemon=True)
        exp_state["thread"].start()
        exp_update()

    def exp_reset():
        was = exp.running
        exp.pause()
        exp_write("종료")
        exp.reset()
        exp_state["seg_map"] = None
        exp_go.configure(text="▶ 측정", bg="#2f9e44")
        if was:
            exp_toggle()
        exp_update()

    def exp_seg_changed(_e=None):
        # 사냥터/파티 인원을 바꾸면: 지금까지 기록을 마감하고 새로 측정 (사냥터별로 나눠서 비교하려고)
        if exp_state["seg_map"] is None:
            cfg["exp_map"], cfg["exp_party"] = exp_map.get().strip(), exp_party.get()
            save_config(cfg)
            return
        if (exp_map.get().strip() or "(이름 없음)") == exp_state["seg_map"] and exp_party.get() == exp_state["seg_party"]:
            return
        exp_reset()
        exp_begin_segment()
        info(f"새 측정 시작: {exp_state['seg_map']} / 파티 {exp_state['seg_party']}명")


    def exp_open_log():
        if not os.path.exists(EXP_LOG_PATH):
            info("아직 경험치 기록이 없어요. 1분 이상 측정하면 생겨요.")
            return
        try:
            os.startfile(EXP_LOG_PATH)  # noqa: S606 (윈도우)
        except Exception:  # noqa: BLE001
            info("기록 파일: " + EXP_LOG_PATH)

    def exp_update():
        t = exp.total_time()
        if not exp.running and t == 0 and exp.gained_exp() == 0:
            txt = ("⏱ [영역]으로 경험치 숫자 지정 → [▶ 측정] (F7)" if not cfg.get("exp_region")
                   else "⏱ 경험치 측정 준비됨 → [▶ 측정] 또는 F7")
        else:
            txt = (f"⏱ {fmt_hms(t)}   " + (f"Lv{exp.level}  " if exp.level else "") + f"+{exp.gained_exp():,} ({exp.gained_pct:.2f}%)   "
                   f"시간당 {exp.rate():,.0f} ({exp.pct_rate():.1f}%)   레벨업까지 {fmt_eta(exp.eta_sec())}")
            if exp.levelups:
                txt += f"   🎉 레벨업 {exp.levelups}번"
            if exp.running and exp.fails >= 3:
                txt += "\n⚠ 경험치 숫자를 못 읽고 있어요 → [영역]으로 다시 지정"
            elif not exp.running:
                txt += "   (일시정지)"
        exp_lbl.configure(text=txt)

    def exp_tick():
        exp_update()
        if exp.running and time.time() - exp_state["last_log"] >= 60:
            exp_state["last_log"] = time.time()
            exp_write("중간")
        root.after(1000, exp_tick)

    def exp_show(on=None):
        on = (not exp_state["shown"]) if on is None else on
        if on and not exp_state["shown"]:
            exp_bar.pack(fill="x", before=body)
        elif not on and exp_state["shown"]:
            exp_bar.pack_forget()
        exp_state["shown"] = on
        cfg["exp_show"] = on
        save_config(cfg)

    ebtn("영역", lambda: select_region("exp_region"))
    ebtn("리셋", exp_reset)
    exp_go = ebtn("▶ 측정", exp_toggle, "#2f9e44")

    body = tk.Text(root, bg="#14161c", fg="#e8ecf4", bd=0, wrap="word",
                   font=("Malgun Gothic", cfg["font_size"]), padx=10, pady=8)
    # --- 내 채팅 보내기 (한국어 -> 외국어) -----------------------------------
    from tkinter import ttk
    send_bar = tk.Frame(root, bg="#20232c")
    send_bar.pack(side="bottom", fill="x")
    send_result = tk.Label(root, text="아래 칸에 한국어로 쓰고 Enter → 번역돼서 자동 복사돼요 (게임에서 Ctrl+V)",
                           bg="#181b22", fg="#8f98ab", anchor="w", justify="left",
                           font=("Malgun Gothic", 9), padx=8, pady=4, wraplength=520, cursor="hand2")
    send_result.pack(side="bottom", fill="x")
    lang_var = tk.StringVar(value=cfg.get("send_lang", "영어"))
    lang_box = ttk.Combobox(send_bar, textvariable=lang_var, values=list(LANG_CHOICES),
                            width=8, state="readonly", font=("Malgun Gothic", 9))
    lang_box.pack(side="left", padx=(6, 3), pady=5)
    send_entry = tk.Entry(send_bar, bg="#2d3240", fg="#ffffff", insertbackground="white",
                          bd=0, font=("Malgun Gothic", 10))
    send_entry.pack(side="left", fill="x", expand=True, padx=3, pady=5, ipady=3)
    send_state = {"seq": 0, "last": ""}

    def copy_text(text):
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()

    def show_send(text, note, color="#ffffff"):
        send_state["last"] = text
        send_result.configure(text=f"{text}\n{note}", fg=color)

    def do_send(_e=None):
        raw = send_entry.get().strip()
        if not raw:
            return
        lang = lang_var.get()
        cfg["send_lang"] = lang
        code, label = LANG_CHOICES.get(lang, ("en", "영어(English)"))
        prefix, body_text = "", raw
        m = re.match(r"^(/[wWㅈ]\s+\S+\s+)(.*)$", raw)  # /w 닉네임 은 그대로 두기
        if m:
            prefix, body_text = "/w " + m.group(1).split()[1] + " ", m.group(2)
        send_state["seq"] += 1
        seq = send_state["seq"]
        send_result.configure(text="번역 중…", fg="#8f98ab")

        def job():
            try:
                quick = prefix + quick_translate(body_text, "ko", code)
                root.after(0, lambda: (seq == send_state["seq"]) and (
                    copy_text(quick), show_send(quick, "▶ 빠른 번역 복사됨 · AI가 다듬는 중…", "#e8ecf4")))
            except Exception as e:  # noqa: BLE001
                root.after(0, lambda err=str(e): show_send(raw, f"빠른 번역 실패: {err}", "#ff7b7b"))
            if ai.enabled and not cfg.get("ai_paused"):
                try:
                    better = prefix + ai_outbound(ai, body_text, label)
                    if better.strip():
                        root.after(0, lambda: (seq == send_state["seq"]) and (
                            copy_text(better), show_send(better, "✦ AI 번역 복사됨 → 게임에서 Ctrl+V", "#7ee08a")))
                except Exception as e:  # noqa: BLE001
                    dlog("OUT-AI-FAIL", repr(e))
        threading.Thread(target=job, daemon=True).start()
        send_entry.delete(0, "end")

    send_entry.bind("<Return>", do_send)
    send_result.bind("<Button-1>", lambda e: send_state["last"] and (
        copy_text(send_state["last"]), status.configure(text="다시 복사됨", fg="#ffcc66")))
    tk.Button(send_bar, text="번역·복사", command=do_send, bg="#3b6ef5", fg="white", bd=0,
              activebackground="#5a86ff", font=("Malgun Gothic", 9), padx=8).pack(side="right", padx=6, pady=5)

    body.pack(fill="both", expand=True)
    body.tag_configure("name", foreground="#ffb547", font=("Malgun Gothic", cfg["font_size"], "bold"))
    body.tag_configure("orig", foreground="#6b7385", font=("Segoe UI", max(8, cfg["font_size"] - 2)))
    body.tag_configure("info", foreground="#7fb4ff")
    body.tag_configure("msg", foreground="#ffffff", lmargin1=0, lmargin2=28)
    body.tag_configure("watchbar", background="#ffd23f", foreground="#1a1a1a",
                       font=("Malgun Gothic", max(8, cfg["font_size"] - 1), "bold"))
    body.tag_configure("watchmsg", background="#3d3410", foreground="#fff3b0")
    body.tag_configure("gap", font=("Malgun Gothic", 3))
    body.tag_configure("chan", foreground="#8f98ab", font=("Segoe UI", max(8, cfg["font_size"] - 2)))
    body.tag_configure("err", foreground="#ff7b7b")
    # 종류별 배경색(확성기류)·글자색(파티·귓속말 등) - 게임 채팅창 색과 비슷하게, 어두운 창에서 잘 보이게
    style_tags = set()
    for _k, _bg in {"확성기": "#3d2533", "고확": "#3f3410", "특확": "#123b37"}.items():
        body.tag_configure("bg_" + _k, background=_bg)
        style_tags.add("bg_" + _k)
    _talk_font = ("Malgun Gothic", cfg["font_size"] + 1, "bold")       # 일반·파티 등 대화는 크고 굵게
    _mega_font = ("Malgun Gothic", max(8, cfg["font_size"] - 1))       # 확성기류는 살짝 작게
    for _k, _fg in {"파티": "#ff8fcf", "귓속말": "#8dff6a", "길드": "#c9a6ff", "친구": "#ffbf4d",
                    "확성기": "#f3d3e3", "고확": "#f5e6b8", "특확": "#c8f2ec", "일반": "#ffffff"}.items():
        body.tag_configure("tx_" + _k, foreground=_fg,
                           font=_mega_font if _k in ("확성기", "고확", "특확") else _talk_font)
        style_tags.add("tx_" + _k)
    # 대화 줄 왼쪽 색 막대 (확성기 사이에서도 일반·파티 채팅이 눈에 띄게)
    for _k, _c in {"일반": "#e8ecf4", "파티": "#ff5fb8", "귓속말": "#5dff3a", "길드": "#b38bff", "친구": "#ffa51f"}.items():
        body.tag_configure("bar_" + _k, foreground=_c, font=("Malgun Gothic", cfg["font_size"] + 1, "bold"))
        style_tags.add("bar_" + _k)
    body.tag_raise("watchmsg")
    body.tag_raise("watchbar")
    for _k, _c in KIND_COLORS.items():
        body.tag_configure("k_" + _k, foreground=_c, font=("Malgun Gothic", max(8, cfg["font_size"] - 1), "bold"))
    body.configure(state="disabled")

    entries = []  # (name, translated, original)

    name_tags = []

    def copy_name(name, whisper):
        text = f"/w {name} " if whisper else name
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        prev = status.cget("text"), status.cget("fg")
        status.configure(text=f"복사됨: {text.strip()}", fg="#ffcc66")
        root.after(1500, lambda: status.configure(text=prev[0], fg=prev[1]))

    def alert_sound():
        if not cfg.get("watch_sound", True):
            return
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:  # noqa: BLE001
            root.bell()

    def check_watch(e, sound=True):
        if e[3] or e[9]:
            return
        res = watch_match(cfg.get("watch") or [], [e[1], e[2], e[10]], e[8])
        if res:
            w, note = res
            e[9] = w["keyword"] + (f" · {note}" if note else "")
            if sound:
                alert_sound()
            status.configure(text=f"★ 찜 매물: {w['keyword']} {note}  ← 닉네임 오른쪽 클릭 = 귓", fg="#ffd23f")

    def open_watch():
        win = tk.Toplevel(root)
        win.title("찾는 물건 (찜)")
        win.attributes("-topmost", True)
        win.configure(bg="#14161c")
        win.geometry("420x360")
        tk.Label(win, text="판매 글(S>)에 이 물건이 나오면 노랗게 표시 + 소리\n"
                           "최대 가격은 비워두면 가격 상관없이 알림 (예: 30 = 30만, 1.2m, 300k)",
                 bg="#14161c", fg="#9aa3b5", justify="left", font=("Malgun Gothic", 9)).pack(anchor="w", padx=10, pady=(8, 4))
        lst = tk.Listbox(win, bg="#20232c", fg="#e8ecf4", bd=0, font=("Malgun Gothic", 10),
                         selectbackground="#3b6ef5", height=8)
        lst.pack(fill="both", expand=True, padx=10)

        def refresh():
            lst.delete(0, "end")
            for w in cfg.get("watch") or []:
                lim = w.get("max_price")
                lst.insert("end", f"{w['keyword']}   " + (f"≤ {format_meso(lim)}" if lim else "(가격 상관없음)"))

        row = tk.Frame(win, bg="#14161c")
        row.pack(fill="x", padx=10, pady=6)
        tk.Label(row, text="물건", bg="#14161c", fg="#e8ecf4", font=("Malgun Gothic", 9)).pack(side="left")
        kw = tk.Entry(row, bg="#2d3240", fg="white", insertbackground="white", bd=0, width=16, font=("Malgun Gothic", 10))
        kw.pack(side="left", padx=4, ipady=2)
        tk.Label(row, text="최대가", bg="#14161c", fg="#e8ecf4", font=("Malgun Gothic", 9)).pack(side="left")
        pr = tk.Entry(row, bg="#2d3240", fg="white", insertbackground="white", bd=0, width=8, font=("Malgun Gothic", 10))
        pr.pack(side="left", padx=4, ipady=2)

        def add(_e=None):
            k = kw.get().strip()
            if not k:
                return
            item = {"keyword": k, "max_price": parse_price_input(pr.get())}
            # 한국어로 쓰면 영어 원문/줄임말도 같이 찾도록 별칭 추가
            item["aliases"] = WATCH_ALIASES.get(re.sub(r"\s+", "", k), [])
            cfg.setdefault("watch", []).append(item)
            save_config(cfg)
            kw.delete(0, "end")
            pr.delete(0, "end")
            refresh()
            for e in entries:  # 이미 떠 있는 글도 다시 확인
                e[9] = None
                check_watch(e, sound=False)
            render()

        def remove():
            sel = lst.curselection()
            if sel:
                del cfg["watch"][sel[0]]
                save_config(cfg)
                refresh()
                for e in entries:
                    e[9] = None
                    check_watch(e, sound=False)
                render()

        kw.bind("<Return>", add)
        pr.bind("<Return>", add)
        bb = tk.Frame(win, bg="#14161c")
        bb.pack(fill="x", padx=10, pady=(0, 8))
        for t, c in (("추가", add), ("선택 삭제", remove), ("닫기", win.destroy)):
            tk.Button(bb, text=t, command=c, bg="#2d3240", fg="#e8ecf4", bd=0,
                      font=("Malgun Gothic", 9), padx=10).pack(side="left", padx=3)
        snd = tk.BooleanVar(value=cfg.get("watch_sound", True))

        def toggle_snd():
            cfg["watch_sound"] = snd.get()
            save_config(cfg)
        tk.Checkbutton(bb, text="소리", variable=snd, command=toggle_snd, bg="#14161c", fg="#e8ecf4",
                       selectcolor="#2d3240", activebackground="#14161c", font=("Malgun Gothic", 9)).pack(side="right")
        refresh()
        kw.focus_set()

    def render():
        body.configure(state="normal")
        body.delete("1.0", "end")
        for t in name_tags:
            body.tag_delete(t)
        name_tags.clear()
        hidden = set(cfg.get("hide_kinds") or [])
        view = view_var.get()
        allowed = VIEW_TABS.get(view)

        def ok(e):
            if e[3] == "hidden":
                return False
            if e[4] and e[4] in hidden:
                return False
            if allowed is None:
                return True
            if e[3]:
                return False
            if allowed in ("sell", "buy"):
                return e[8] == allowed
            if allowed == "watch":
                return bool(e[9])
            return (e[4] or "일반") in allowed
        shown = [e for e in entries if ok(e)]
        for e in shown[-cfg["max_lines"]:]:
            name, tr, orig, tag, kind, _eid, is_ai, ch, _trade, hit = e[:10]
            if tag:
                body.insert("end", tr + "\n", tag)
                continue
            # 게임처럼 종류별로 색 구분: 확성기·고확·특확 = 배경색, 파티·귓속말·길드·친구 = 글자색
            kname = kind or "일반"
            block_start = body.index("end-1c")
            # 1줄: [종류] 닉네임
            if hit:
                body.insert("end", f" ★ 찜: {hit} ", "watchbar")
                body.insert("end", "\n")
            if ("bar_" + kname) in style_tags:
                body.insert("end", "▌", "bar_" + kname)
            if kind:
                body.insert("end", f"[{kind}] ", "k_" + kind)
            if name:
                ntag = f"nm_{len(name_tags)}"
                name_tags.append(ntag)
                body.insert("end", f"[{name}]", ("name", ntag))
                body.tag_bind(ntag, "<Button-1>", lambda e, n=name: copy_name(n, whisper=False))
                body.tag_bind(ntag, "<Button-3>", lambda e, n=name: copy_name(n, whisper=True))
                body.tag_bind(ntag, "<Enter>", lambda e: body.configure(cursor="hand2"))
                body.tag_bind(ntag, "<Leave>", lambda e: body.configure(cursor=""))
                if ch:
                    body.insert("end", "  " + ch, "chan")
                body.insert("end", "\n")
            # 닉네임이 없는 줄 = 앞 채팅이 길어서 줄바꿈된 부분 -> 내용만 표시
            # 2줄: 번역된 대화 내용
            msg_start = body.index("end-1c")
            body.insert("end", ("  ✧ " if is_ai == "mem" else "  ✦ " if is_ai else "  ▶ ") + tr + "\n", ("msg", "watchmsg") if hit else "msg")
            if not hit and ("tx_" + kname) in style_tags:
                body.tag_add("tx_" + kname, msg_start, "end-1c")
            # 3줄: 원문 (선택)
            if cfg["show_original"] and orig and orig != tr:
                body.insert("end", "     " + orig + "\n", "orig")
            if ("bg_" + kname) in style_tags:
                body.tag_add("bg_" + kname, block_start, "end-1c")
            body.insert("end", "\n", "gap")
        body.configure(state="disabled")
        body.see("end")

    def info(msg, tag="info"):
        entries.append([None, msg, None, tag, None, 0, False, None, None, None, None])
        render()

    # --- 영역 선택 ---------------------------------------------------------
    def select_region(target="region"):
        is_exp = target == "exp_region"
        was_running = state["running"]
        if not is_exp:
            stop()
        sel = tk.Toplevel(root)
        sel.attributes("-fullscreen", True)
        sel.attributes("-alpha", 0.30)
        sel.attributes("-topmost", True)
        sel.configure(bg="black", cursor="cross")
        canvas = tk.Canvas(sel, bg="black", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_text(sel.winfo_screenwidth() // 2, 60, fill="white",
                           font=("Malgun Gothic", 18, "bold"),
                           text=("경험치 숫자(예: 12,345 [12.34%])가 있는 부분만 드래그하세요  (취소: ESC)" if is_exp
                                 else "채팅 글자가 있는 부분만 드래그하세요  (취소: ESC)"))
        pos = {}

        def down(e):
            pos["x0"], pos["y0"] = e.x_root, e.y_root
            pos["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#ffcc00", width=3)

        def move(e):
            if "rect" in pos:
                x0 = pos["x0"] - sel.winfo_rootx()
                y0 = pos["y0"] - sel.winfo_rooty()
                canvas.coords(pos["rect"], x0, y0, e.x, e.y)

        def up(e):
            x0, y0, x1, y1 = pos.get("x0", 0), pos.get("y0", 0), e.x_root, e.y_root
            sel.destroy()
            left, top = min(x0, x1), min(y0, y1)
            w, h = abs(x1 - x0), abs(y1 - y0)
            if w < (20 if is_exp else 40) or h < (8 if is_exp else 15):
                info("영역이 너무 작아요. 다시 선택하세요.", "err")
                return
            cfg[target] = [left, top, w, h]
            save_config(cfg)
            if is_exp:
                info(f"경험치 영역 저장 완료 ({w}x{h}). [▶ 측정] 또는 F7로 시작하세요.")
                exp_show(True)
                return
            info(f"채팅창 영역 저장 완료 ({w}x{h}). F8로 시작하세요.")
            if was_running:
                start()

        canvas.bind("<ButtonPress-1>", down)
        canvas.bind("<B1-Motion>", move)
        canvas.bind("<ButtonRelease-1>", up)
        sel.bind("<Escape>", lambda e: sel.destroy())
        sel.focus_force()

    # --- 작업 스레드 -------------------------------------------------------
    pending = [None]
    owner = {}   # 화면 줄 키 -> 띄운 항목 id
    trade_log = TradeLog(enabled=cfg.get("trade_log", True))  # 팝니다/삽니다 채팅 기록
    # 메소워치가 프로그램 접속을 막아서(403) 기본은 꺼둠. 시세는 Claude 브리핑이 크롬으로 가져와 기록함.
    MarketCollector(enabled=cfg.get("market_collect", False)).start()
    MarketImporter(cfg).start()  # 크롬이 받은 메소워치 전체 시세 파일 -> market.db
    retry = []   # 번역 실패해서 다시 시도할 줄 (id, 보호된 문장, 자리표시자)
    fixed = {}   # 항목 id -> 마지막으로 보낸 보완 정보

    def worker():
        while state["running"]:
            started = time.time()
            try:
                lines = ocr.read_lines(cfg["region"])
                batch = []  # (name, msg, protected, slots)
                ordered = []  # 화면 순서 그대로 내보내기: ("now", 항목) 또는 ("tr", batch 번호)
                fresh = set(seen.new_lines([raw for raw, _r, _i in lines]))
                cands = []
                for idx, (raw, rect, items) in enumerate(lines):
                    if idx not in fresh:
                        # 이미 띄운 줄을 이번엔 더 잘 읽었으면 (닉네임/채널/팝니다 표시) 그 줄을 보완
                        okey = seen.match_key(raw)
                        eid0 = owner.get(okey)
                        if eid0:
                            n2, m2, c2 = parse_line(raw)
                            t2 = trade_of(normalize_trade(m2), strong=True)
                            got = (n2 if n2 and len(n2) >= 3 else None, c2, t2)
                            if any(got) and fixed.get(eid0) != got:
                                fixed[eid0] = got
                                out_q.put(("__fix__", eid0) + got)
                        continue
                    cur_key = normalize(raw)
                    if _TOOLTIP_RE.match(raw):
                        continue  # 아이템 설명창이 겹쳐 보인 것 (MaxHP : +10 등)
                    name, msg, ch = parse_line(raw)
                    width0 = ocr.last_img.width if getattr(ocr, "last_img", None) is not None else 0
                    if (not name and not pending[0] and idx > 0 and (idx - 1) not in fresh and lines[idx - 1][1]
                            and width0 and lines[idx - 1][1][0] + lines[idx - 1][1][2] >= width0 * 0.85
                            and len(msg.split()) <= 4 and not trade_of(msg)):
                        dlog("DROP-WRAP", msg)  # 이미 띄운 긴 줄의 줄바꿈 꼬리 ('eople', 'ngrats!') -> 따로 안 띄움
                        continue
                    if ch and not name and not pending[0]:
                        report_issue("닉네임 못 읽음 (배지는 찾음)", raw, img=ocr.last_img, rect=rect)
                    if name and not msg.strip():
                        pending[0] = (name, ch, items)  # 닉네임만 읽힌 줄 -> 다음 줄이 대화
                        continue
                    if not name and pending[0]:
                        name, ch, p_items = pending[0]
                        items = list(p_items) + [i for i in items if i not in p_items]
                    pending[0] = None
                    msg = normalize_trade(merge_item_links(msg, items))
                    if re.match(r"^[^\[\]]{2,40}\]", msg):  # 앞쪽 '[' 를 놓친 아이템 링크 복구
                        msg = "[" + msg
                    msg = re.sub(r"[@©®]{3,}", "", msg).strip()  # 아이콘을 @@@@ 로 읽은 찌꺼기
                    # 긴 채팅이 줄바꿈된 뒷부분 ('g 800k') -> 바로 윗줄에 이어 붙임
                    width = ocr.last_img.width if getattr(ocr, "last_img", None) is not None else 0
                    if (not name and cands and cands[-1]["idx"] == idx - 1 and len(cands[-1]["msg"]) >= 25
                            and cands[-1]["right"] >= width * 0.85  # 윗줄이 오른쪽 끝까지 꽉 찼을 때만 = 줄바꿈
                            and not re.match(r"^\s*[SBTL]\s?>", msg)
                            and not _NOTICE_RE.search(msg)):  # 아랫줄 [공지] 가 윗줄 채팅에 붙던 문제
                        cands[-1]["msg"] += ("" if cands[-1]["msg"].endswith(" ") else " ") + msg
                        cands[-1]["msg"] = normalize_trade(cands[-1]["msg"])  # 줄바꿈에 잘린 금액 '5' + '00K' -> 500K
                        cands[-1]["keys"].append(cur_key)
                        cands[-1]["idx"] = idx
                        cands[-1]["right"] = (rect[0] + rect[2]) if rect else 0
                        continue
                    cands.append({"idx": idx, "keys": [cur_key], "name": name, "msg": msg, "ch": ch,
                                  "kind": classify_line(ocr.last_img, rect),
                                  "right": (rect[0] + rect[2]) if rect else 0})
                for c in cands:
                    cur_key, name, msg, ch, kind = c["keys"], c["name"], c["msg"], c["ch"], c["kind"]
                    dlog("LINE", kind, repr(name), ch, msg)
                    letters = len(re.findall(r"[A-Za-z가-힣0-9?!]", msg))
                    if not name and not re.search(r"[A-Za-z가-힣]", msg):
                        dlog("DROP-JUNK", msg)
                        continue  # 닉네임 없이 숫자·기호만 있는 줄 ('20 461', '452') = 다른 UI 숫자를 읽은 찌꺼기
                    if not name and not HANGUL_RE.search(msg) and not trade_of(msg) and text_quality(msg) < 0.55:
                        dlog("DROP-JUNK", msg)
                        continue  # 닉네임도 없고 글자가 엉망인 줄 = OCR 찌꺼기
                    if (not name and ":" not in msg and not HANGUL_RE.search(msg) and not trade_of(msg)
                            and is_my_nametag(msg, cfg.get("my_name"))):
                        dlog("DROP-NAMETAG", msg)
                        state["nametag_n"] = state.get("nametag_n", 0) + 1
                        if state["nametag_n"] == 15:  # 계속 이름표만 읽히면 한 번 알려줌
                            out_q.put((None, "채팅창 대신 게임 화면(캐릭터 이름표)이 읽히고 있어요. "
                                             "F9로 채팅창 영역을 다시 선택해 주세요", None, "err", None, 0))
                        continue  # 내 캐릭터 이름표를 채팅으로 잘못 읽은 줄 (채팅창 영역이 게임 화면에 겹침)
                    if not name and not trade_of(msg) and nametag_like(msg, cfg.get("my_name")):
                        dlog("DROP-NAMETAG2", msg)
                        continue  # 투명한 채팅창 뒤로 지나간 내 캐릭터 이름표 조각
                    if letters < (1 if name else 3):
                        continue  # OCR 찌꺼기 (닉네임이 있는 줄은 "dd", "ty" 같은 짧은 말도 표시)
                    if kind == "공지" and cfg.get("hide_notice", True):
                        continue  # 노란 글씨 게임 공지 (글자가 깨져도 색으로 판별해서 숨김)
                    if ch in ("받은 귓", "보낸 귓"):
                        kind = "귓속말"
                    elif kind == "파티" and not name:
                        continue  # 닉네임 없는 분홍 줄 = 파티 시스템 메시지 (누가 들어옴/나감 등) -> 숨김
                    elif not name and _NOTICE_RE.search(msg):
                        kind = "공지"  # 게임 공지·가챠 알림
                        if cfg.get("hide_notice", True):
                            continue  # 아예 안 보여줌 (번역도 안 함)
                    if kind not in ("귓속말", "공지"):
                        trade_log.add(name, ch, kind, msg)  # 팝니다/삽니다 채팅만 시세기록에 저장 (AI 안 씀)
                    if ch == "보낸 귓" or (name and cfg.get("my_name") and name.lower() == cfg["my_name"].lower()):
                        ordered.append((cur_key, "now", (name, msg, msg, None, kind, ch)))
                        continue
                    plain = re.sub(r"\[[^\]]*\]", "", msg)
                    if cfg["skip_korean_lines"] and plain.strip() and len(HANGUL_RE.findall(plain)) > len(plain) * 0.3:
                        ordered.append((cur_key, "now", (name, msg, msg, None, kind, ch)))
                        continue
                    if cfg.get("ai_memory", True):
                        saved = memory.get(msg)
                        if saved and not ai_output_ok(msg, saved):
                            saved = None  # 저장된 번역의 가격이 원문과 다르면 쓰지 않음 (다시 번역)
                        if saved:  # 예전에 AI가 번역한 똑같은 채팅 -> Claude·구글 없이 바로
                            ordered.append((cur_key, "mem", (name, saved, msg, None, kind, ch)))
                            continue
                    protected, slots = glossary.protect(msg)
                    ordered.append((cur_key, "tr", len(batch)))
                    batch.append((name, msg, protected, slots, kind, ch))
                err = None
                translated = []
                # 지난번에 번역 실패한 줄 다시 시도 (성공하면 화면의 그 줄만 바꿔줌)
                if retry and time.time() - state.get("retry_at", 0) > 4:
                    state["retry_at"] = time.time()
                    rt, _rerr = translator.translate_many([r[1] for r in retry])
                    still = []
                    for i, (reid, rprot, rslots) in enumerate(retry):
                        if i in translator.failed:
                            still.append((reid, rprot, rslots))
                        else:
                            out_q.put(("__retr__", reid, glossary.restore(rt[i], rslots)))
                    retry[:] = still[-30:]
                if batch:
                    translated, err = translator.translate_many([b[2] for b in batch])
                    failed_now = set(translator.failed)
                for okeys, how, val in ordered:
                    eid = next_id()
                    for okey in okeys:
                        owner[okey] = eid
                    if len(owner) > 400:
                        for k in list(owner)[:100]:
                            del owner[k]
                    if how in ("now", "mem"):
                        name, tr, msg, tag, kind, ch = val
                        out_q.put((name, tr, msg, tag, kind, eid, ch, msg, trade_of(msg), how == "mem"))
                    else:
                        name, msg, prot, slots, kind, ch = batch[val]
                        kor = glossary.restore(prot, slots)  # 용어만 한국어로 바꾼 원문 (찜 검색용)
                        tr_text = glossary.restore(translated[val], slots)
                        dlog("TR", msg, "=>", tr_text)
                        if val in failed_now:
                            retry.append((eid, prot, slots))
                        out_q.put((name, tr_text, msg, None, kind, eid, ch,
                                   kor, trade_of(msg)))
                        if ai.enabled and not cfg.get("ai_paused"):
                            # 용어 사전을 먼저 적용한 문장도 같이 줘서 AI가 노작·크뎀·장공·금액 표기를 그대로 쓰게
                            ai_q.put((eid, msg + (f"  ⟪용어 적용: {kor}⟫" if kor and kor != msg else "")))
                if batch:
                    if err and err != state.get("last_err"):
                        out_q.put((None, err, None, "err", None, 0))
                    state["last_err"] = err
            except Exception as e:  # noqa: BLE001
                out_q.put((None, f"오류: {type(e).__name__}: {e}", None, "err", None, 0))
                time.sleep(2)
            wait = cfg["interval_sec"] - (time.time() - started)
            if wait > 0:
                time.sleep(wait)

    def ai_worker():
        while cfg.get("ai_paused"):  # AI 꺼져 있으면 연결 테스트도 안 함 (사용량 0)
            time.sleep(1)
        try:
            test = ai.translate_batch(["gz tyfp"])
            out_q.put((None, f"✦ AI 번역 연결 확인 완료 (테스트: gz tyfp → {test[0]})", None, "info", None, 0))
        except Exception as e:  # noqa: BLE001
            out_q.put((None, f"✦ AI 번역 연결 실패: {e}  → 구글 번역만 표시돼요", None, "err", None, 0))
        fails = 0
        while True:
            job = [ai_q.get()]
            if cfg.get("ai_paused"):
                continue  # AI 꺼둔 동안은 구글 번역만
            time.sleep(float(cfg.get("ai_batch_wait", 2.0)))  # 같이 올라온 줄 모아서 한 번에 (호출 횟수 = 사용량 절약)
            while not ai_q.empty() and len(job) < 25:
                job.append(ai_q.get_nowait())
            try:
                outs = ai.translate_batch([m for _i, m in job])
                for (eid, _m), tr in zip(job, outs):
                    if tr and not ai_output_ok(_m, tr):
                        dlog("AI-REJECT", _m, "=>", tr)
                        continue  # 가격이 원문과 다르면 -> 구글 번역 유지 (저장도 안 함)
                    if tr:
                        out_q.put(("__update__", eid, tr))
                fails = 0
            except Exception as e:  # noqa: BLE001
                dlog("AI-FAIL", repr(e))
                fails += 1
                if fails in (1, 5):
                    out_q.put((None, f"AI 번역 실패 (구글 번역으로 표시 중): {e}", None, "err", None, 0))
                time.sleep(min(30, 3 * fails))

    if ai.enabled:
        threading.Thread(target=ai_worker, daemon=True).start()

    def pump():
        changed = False
        if OCR_NOTICE[0]:
            info(OCR_NOTICE[0], "err")
            OCR_NOTICE[0] = None
        while True:
            try:
                item = out_q.get_nowait()
            except queue.Empty:
                break
            if item[0] == "__retr__":
                for e in reversed(entries):
                    if e[5] == item[1]:
                        if not e[6]:  # AI 번역이 이미 왔으면 그대로 둠
                            e[1] = item[2]
                            check_watch(e)
                        break
                changed = True
                continue
            if item[0] == "__fix__":
                _f, fid, n2, c2, t2 = item
                for e in reversed(entries):
                    if e[5] == fid:
                        if n2 and not e[0]:
                            e[0] = n2
                        if c2 and not e[7]:
                            e[7] = c2
                        if t2 and e[8] != t2:  # S>/B> 를 확실히 읽었으면 그걸로 바로잡기
                            e[8] = t2
                            e[9] = None
                            check_watch(e)
                        break
                changed = True
                continue
            if item[0] == "__update__":
                for e in reversed(entries):
                    if e[5] == item[1]:
                        if re.search(r"번역\s*불가|해석\s*불가|알\s*수\s*없", item[2]) and len(item[2]) < 30:
                            e[3] = "hidden"  # AI도 못 읽은 찌꺼기 줄은 숨김
                            break
                        e[1], e[6] = item[2], True
                        if cfg.get("ai_memory", True) and e[2]:
                            memory.add(e[2], item[2])
                        left = english_left(item[2], e[2])
                        if left and e[0] and left != [e[0]]:
                            left = [w for w in left if w.lower() != (e[0] or "").lower()]
                        if left:
                            report_issue("번역에 영어가 남음: " + ", ".join(left[:5]), e[2] or "", e[2] or "", item[2])
                        check_watch(e)
                        break
            else:
                ex = list(item) + [None] * (10 - len(item))
                # [0닉 1번역 2원문 3태그 4종류 5id 6AI 7채널 8거래 9찜 10검색용]
                e = list(ex[:6]) + ["mem" if ex[9] else False, ex[6], ex[8], None, ex[7]]
                entries.append(e)
                check_watch(e)
            changed = True
        if len(entries) > 200:
            del entries[:-200]
        if changed:
            render()
        root.after(150, pump)

    def start():
        if state["running"]:
            return
        if ocr is None:
            messagebox.showerror("OCR 오류",
                                 "윈도우 OCR을 불러오지 못했어요.\n설치하기.bat를 다시 실행해 주세요.\n\n" + ocr_error)
            return
        if not cfg.get("region"):
            info("먼저 F9(영역 선택)로 채팅창을 지정하세요.", "err")
            return
        state["running"] = True
        state["nametag_n"] = 0
        power_btn.configure(text="■ 끄기", bg="#c92a2a", activebackground="#e03131")
        status.configure(text="번역 중…  (F8 정지)", fg="#7ee08a")
        state["worker"] = threading.Thread(target=worker, daemon=True)
        state["worker"].start()

    def stop():
        state["running"] = False
        power_btn.configure(text="▶ 켜기", bg="#2f9e44", activebackground="#40c057")
        status.configure(text="정지됨  (F8 시작, F9 영역)", fg="#9aa3b5")

    def toggle():
        stop() if state["running"] else start()

    def reload_glossary():
        try:
            glossary.reload()
            translator.cache.clear()
            info("용어 사전을 다시 불러왔어요.")
        except Exception as e:  # noqa: BLE001
            info(f"사전 오류: {e}", "err")

    def toggle_orig():
        cfg["show_original"] = not cfg["show_original"]
        save_config(cfg)
        render()

    def autosave():
        memory.save()
        root.after(30_000, autosave)
    root.after(30_000, autosave)

    def on_close():
        if exp.running:
            exp.pause()
            exp_write("종료")
        memory.save()
        state["running"] = False
        cfg["overlay_geometry"] = root.geometry()
        save_config(cfg)
        root.destroy()

    btn("✕", on_close)
    btn("원문", toggle_orig)
    btn("★찜", open_watch)

    def toggle_ai():
        cfg["ai_paused"] = not cfg.get("ai_paused")
        save_config(cfg)
        ai_btn.configure(text="AI 꺼짐" if cfg["ai_paused"] else "AI 켜짐")
        info("AI 번역 일시정지 (구글 번역만, Claude 사용량 0)" if cfg["ai_paused"]
             else f"AI 번역 다시 켬 (이번 실행 AI 호출 {AITranslator.calls}회)")
    if ai.enabled:
        ai_btn = btn("AI 꺼짐" if cfg.get("ai_paused") else "AI 켜짐", toggle_ai)
    btn("사전", reload_glossary)
    btn("경험치", lambda: exp_show())
    btn("영역(F9)", select_region)
    power_btn.configure(command=toggle)

    root.bind("<F8>", lambda e: toggle())
    root.bind("<F9>", lambda e: select_region())
    root.bind("<F7>", lambda e: exp_toggle())
    root.protocol("WM_DELETE_WINDOW", on_close)

    # 게임 창에 포커스가 있어도 F8/F9가 먹도록 (가능할 때만)
    try:
        import keyboard
        keyboard.add_hotkey("f8", lambda: root.after(0, toggle))
        keyboard.add_hotkey("f9", lambda: root.after(0, select_region))
        keyboard.add_hotkey("f7", lambda: root.after(0, exp_toggle))
    except Exception:  # noqa: BLE001
        info("전역 단축키를 못 켰어요. 오버레이 버튼을 쓰거나 관리자 권한으로 실행하세요.", "err")

    set_view(view_var.get())
    if not cfg.get("my_name"):  # 처음 쓰는 사람: 게임 닉네임 물어보기 (내 채팅은 번역 안 하려고)
        try:
            from tkinter import simpledialog
            nm = simpledialog.askstring("메이플 채팅 번역기", "게임 캐릭터 닉네임을 입력하세요\n(내가 친 채팅은 번역하지 않아요)", parent=root)
            if nm and nm.strip():
                cfg["my_name"] = nm.strip()
                save_config(cfg)
        except Exception:  # noqa: BLE001
            pass
    info(f"{cfg.get('my_name') or ''}님 반가워요! 오버레이는 채팅창과 안 겹치는 곳에 두세요.".lstrip("님 "))
    if ai.enabled:
        info("AI 번역 켜짐: ▶ 구글 번역이 먼저 뜨고, 잠시 뒤 ✦ 자연스러운 한국어로 바뀌어요.")
    elif ai.error:
        info("AI 번역 꺼짐: " + ai.error + " (구글 번역만 사용)", "err")
    if ocr_error:
        info("OCR 준비 실패: " + ocr_error, "err")
    elif cfg.get("region"):
        info("저장된 채팅창 영역이 있어요. F8로 바로 시작할 수 있어요.")

    if cfg.get("exp_show"):
        exp_show(True)
    root.after(1000, exp_tick)
    root.after(150, pump)
    root.mainloop()


# ----------------------------------------------------------------------------
def run_test(text):
    glossary = Glossary()
    protected, slots = glossary.protect(text)
    print("원문      :", text)
    print("전처리    :", protected)
    print("자리표시자:", slots)
    translator = Translator("ko")
    out = glossary.restore(translator.translate(protected), slots)
    print("번역      :", out)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--test":
        run_test(" ".join(sys.argv[2:]))
    else:
        if load_config().get("auto_update", True) and "--no-update" not in sys.argv:
            if auto_update():
                os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), "--no-update"])
        run_app()
