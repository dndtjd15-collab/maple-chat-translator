# -*- coding: utf-8 -*-
"""
메이플 채팅 번역기 (MapleStory Classic World chat translator) - 성윤 전용

채팅창 영역을 주기적으로 캡처 -> 윈도우 기본 OCR로 글자 인식
-> 메이플 용어 사전으로 줄임말/아이템 용어 처리 -> 구글 번역(무료)
-> 화면 위 오버레이에 한국어로 표시.

단축키: F9 = 채팅창 영역 선택, F8 = 번역 시작/정지 (오버레이 버튼으로도 가능)
테스트: python maple_translator.py --test "S> Wand MA 60% 175k obo, gz!"
"""
import json
import os
import queue
import re
import sys
import threading
import time
from difflib import SequenceMatcher

APP_VERSION = 7  # 업데이트할 때마다 1씩 올림 (GitHub version.json 과 비교)
UPDATE_BASE = "https://raw.githubusercontent.com/dndtjd15-collab/maple-chat-translator/main/"

APP_DIR = os.path.dirname(os.path.abspath(__file__))
GLOSSARY_PATH = os.path.join(APP_DIR, "maple_glossary.json")
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
    "my_name": "SungWoong",    # 내 채팅은 번역 생략
    "skip_korean_lines": True, # 이미 한국어인 줄은 번역 생략
    "auto_update": True,       # 실행할 때 GitHub에서 새 버전 자동 받기
    "debug": True,             # 문제 확인용 기록(debug_log.txt) 남기기
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
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        print("config.json 저장 실패:", e)


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
            for sec in ("expand", "terms", "terms_case", "names"):
                data.setdefault(sec, {}).update(mine.get(sec) or {})
        clean = lambda d: {k: v for k, v in (d or {}).items() if not k.startswith("_")}
        self.expand = clean(data.get("expand"))
        terms = clean(data.get("terms"))
        terms.update(clean(data.get("names")))
        self.terms = terms
        self.terms_case = clean(data.get("terms_case"))
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

        # 1) 대괄호 아이템 링크: 한글은 그대로, 영어 부분만 번역
        #    [에메랄드 귀고리] -> 그대로 / [Emerald Earrings] -> [에메랄드 귀고리]
        #    [장갑 Attack 주문서] -> 한글 유지 + Attack 만 번역
        def bracket(m):
            inner = m.group(1)
            if not re.search(r"[A-Za-z]", inner):
                return hold(m.group(0))  # 한글/숫자만 -> 통째로 보존
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
                    out.append(hold(part.strip()))  # 한글·숫자·기호는 그대로
            return " [ " + " ".join(out) + " ] "

        text = re.sub(r"\[([^\]]{1,40})\]", bracket, text)

        # 2) 메소 금액 500k / 1.2m / 2.5mil / 300 mil -> 50만 / 120만
        def money(m):
            num = float(m.group(1).replace(",", ""))
            unit = m.group(2).lower()
            value = num * (1_000 if unit.startswith("k") else 1_000_000)
            return hold(format_meso(value))

        text = re.sub(r"(?<![A-Za-z0-9])(\d+(?:[.,]\d+)?)\s*(k|kk|m|mil|mill|million)(?![A-Za-z])",
                      money, text, flags=re.IGNORECASE)

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
        out = re.sub(r"\[\s+", "[", out)
        out = re.sub(r"\s+\]", "]", out)
        return re.sub(r"\s{2,}", " ", out).strip()


def format_meso(value):
    value = int(round(value))
    if value >= 100_000_000:
        eok, rest = divmod(value, 100_000_000)
        man = rest // 10_000
        return f"{eok}억 {man}만 메소" if man else f"{eok}억 메소"
    if value >= 10_000:
        man = value / 10_000
        man_str = f"{man:.1f}".rstrip("0").rstrip(".")
        return f"{man_str}만 메소"
    return f"{value:,} 메소"


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
        last_err = "번역 엔진 없음"
        now = time.time()
        for name, fn in self.engines:
            if self.blocked.get(name, 0) > now:
                continue
            try:
                return fn(text)
            except Exception as e:  # noqa: BLE001
                last_err = f"{name}: {type(e).__name__}"
                # 막힌 엔진은 잠시 쉬게 하고 바로 다음 엔진 시도
                self.blocked[name] = now + (60 if "TooMany" in str(e) else 15)
        raise RuntimeError(last_err)

    def translate_many(self, texts):
        """texts 목록을 번역해서 같은 순서로 돌려줌. 실패하면 None 자리에 원문."""
        results = list(texts)
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
            return results, f"번역 엔진이 모두 잠깐 막혔어요 ({e}). 곧 자동으로 다시 돼요"
        parts = out.split("\n")
        if len(parts) != len(todo):
            # 줄 수가 안 맞으면 한 줄씩 다시 (드문 경우)
            parts = []
            for i in todo:
                try:
                    parts.append(self._request(texts[i]))
                except Exception:  # noqa: BLE001
                    parts.append(texts[i])
        for i, tr in zip(todo, parts):
            tr = tr.strip() or texts[i]
            results[i] = tr
            self.cache[texts[i]] = tr
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
    raw = CHANNEL_RE.sub("", part).strip(" \t:;：@®©*·•|")
    return raw or None


_CH_FIX = str.maketrans({"O": "0", "o": "0", "Z": "2", "z": "2", "S": "5", "s": "5",
                         "I": "1", "l": "1", "B": "8"})


def parse_line(raw):
    """채팅 한 줄을 (닉네임, 대화내용, 채널)로 분리.
    형식 1) 닉네임 CH12 ♥ : 대화   (확성기 - CH 배지 왼쪽이 닉네임, 배지가 채널)
    형식 2) 닉네임 : 대화           (일반/파티)
    배지는 있는데 대화가 없으면 ('닉네임', '', 채널) -> 다음 줄에 대화가 이어짐."""
    line = raw.strip()
    badge = BADGE_RE.search(line)
    if badge and badge.start() <= 26:
        name = _clean_name(line[:badge.start()])
        ch = "CH" + badge.group(1).translate(_CH_FIX)
        rest = line[badge.end():]
        rest = re.sub(r"^\s*[:：;]\s*", "", rest).strip()  # 배지 뒤 콜론 제거 (OCR이 놓쳐도 OK)
        if name:
            return name, rest, ch
    m = LINE_RE.match(line)
    if not m:
        m2 = re.match(r"^\s*([^:：;\s][^:：;]{0,30}?)\s*[:：;]\s*$", line)
        if m2 and _clean_name(m2.group(1)) and len(m2.group(1).split()) <= 2:
            return _clean_name(m2.group(1)), "", None  # 닉네임만 있고 대화는 다음 줄
        return None, line, None
    name = _clean_name(m.group(1))
    msg = m.group(2).strip()
    return (name, msg, None) if name else (None, msg, None)


def normalize(s):
    return re.sub(r"[^a-z0-9가-힣]", "", s.lower())


class SeenLines:
    def __init__(self, size=300):
        self.items = []
        self.size = size

    def is_new(self, line):
        key = normalize(line)
        if len(key) < 3:
            return False
        for old in reversed(self.items[-80:]):
            if old == key or SequenceMatcher(None, old, key).ratio() > 0.88:
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
        if self._mss is None:
            self._mss = mss.mss()
        left, top, width, height = region
        shot = self._mss.grab({"left": left, "top": top, "width": width, "height": height})
        img = Image.frombytes("RGB", shot.size, shot.rgb)
        self._orig = img
        if self.upscale and self.upscale != 1:
            img = img.resize((int(img.width * self.upscale), int(img.height * self.upscale)),
                             Image.LANCZOS)
        return img

    def _ocr(self, img, lang):
        """OCR 후 단어들을 '같은 높이'끼리 다시 묶어서 한 줄로 만든다.
        (CH 배지·하트 아이콘 때문에 윈도우 OCR이 한 줄을 여러 조각으로 쪼개는 문제 해결)"""
        import asyncio
        import winocr
        result = asyncio.run(winocr.to_coroutine(winocr.recognize_pil(img, lang)))
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
            lines.append((text, (int(x0), int(y0), int(x1 - x0), int(y1 - y0))))
        return lines

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
        if self.lang != "ko" and self._ko_supported():
            try:
                ko = self._ocr(img, "ko")
            except Exception:  # noqa: BLE001
                ko = []
        dlog("OCR-EN", [t for t, _r in en])
        dlog("OCR-KO", [t for t, _r in ko])
        out = []
        for text, rect in en:
            items = []
            if rect and ko:
                cy = rect[1] + rect[3] / 2
                for ktext, krect in ko:
                    if not krect:
                        continue
                    kcy = krect[1] + krect[3] / 2
                    if abs(kcy - cy) <= max(rect[3], krect[3]) * 0.6:
                        found = re.findall(r"\[[^\[\]]*[가-힣][^\[\]]*\]", ktext)
                        if not found:  # 대괄호를 못 읽은 경우: 한글 덩어리를 아이템 이름으로
                            found = ["[" + h.strip() + "]" for h in
                                     re.findall(r"[가-힣][가-힣0-9 +\-]*[가-힣]", ktext)]
                        items += [f for f in found if f not in items]
            out.append((text, rect, items))
        return out


# OCR이 S> B> 를 S) 5> B) 8> S» 처럼 잘못 읽는 경우를 원래대로 되돌림
_TRADE_TAG_RE = re.compile(r"(?:(?<=^)|(?<=[\s:;/,\]]))([SsBbTtLlFf5$8])\s?[>)»›\}]\s*")
_TRADE_MAP = {"5": "S", "$": "S", "8": "B"}


def normalize_trade(msg):
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
    ("파티",   (255, 153, 204), "#ff99cc"),
    ("귓속말", (0, 255, 0),     "#7CFC00"),
    ("길드",   (187, 136, 255), "#c39bff"),
    ("친구",   (255, 170, 0),   "#ffaa00"),
]
KIND_COLORS = {k: c for k, _, c in BG_KINDS + TEXT_KINDS}
KIND_COLORS["일반"] = "#d0d6e2"


# 보기 탭: 이름 -> 보여줄 종류 (None = 전부)
VIEW_TABS = {
    "전체": None,
    "일반": {"일반"},
    "파티": {"파티"},
    "고확": {"고확"},
    "특확": {"특확"},
    "확성기": {"확성기", "고확", "특확"},
    "귓속말": {"귓속말"},
}


def _dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


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
    # 1) 배경색 (줄 면적의 25% 이상이 해당 색이면 확성기)
    best, best_ratio = None, 0.0
    for kind, ref, _c in BG_KINDS:
        ratio = sum(1 for p in pixels if _dist(p, ref) < 50) / n
        if ratio > best_ratio:
            best, best_ratio = kind, ratio
    if best and best_ratio > 0.25:
        return best
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
- 직업: sin = 어쌔신, dit/bandit = 시프, I/L = 썬콜, F/P = 불독, cleric = 클레릭, fighter = 파이터, page = 페이지, spearman = 스피어맨, hunter = 헌터, xbow = 사수.
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
- OCR이 글자를 깨뜨린 경우(예: "thats mor thần a." → "that's more than a ...")는 원래 문장을 추측한 뒤 그걸 한국어로 번역하고, 앞에 (추정)을 붙여. 영어를 그대로 출력하지 마.
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
1|[팝니다] 레드 바이퍼 +2, 니암 +4 / [삽니다] 쿰비 29만
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
]


def polish_ai(text):
    """AI가 놓친 거래 표기를 마저 한국어로 바꾸고 중복 표현 정리."""
    for pat, rep in TRADE_FIX:
        text = pat.sub(rep, text)
    text = re.sub(r"(\[(팝니다|삽니다)\])([^\[]*?)\s*\2(?=\s|$|/|,)", r"\1\3", text)
    text = re.sub(r"장공\s*공격력", "장공", text)
    text = re.sub(r"장마\s*마력", "장마", text)
    return re.sub(r"\s{2,}", " ", text).strip()


OUT_PROMPT = """너는 메이플스토리 클래식 월드(GMS, 해외 서버) 채팅 통역사야.
한국 유저(성윤)가 쓴 한국어 채팅을 {lang} 게임 채팅으로 바꿔줘.
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

    def translate_batch(self, msgs):
        payload = "\n".join(f"{i + 1}|{m.replace(chr(10), ' ')}" for i, m in enumerate(msgs))
        if self.engine == "claude_cli":
            import subprocess
            flags = 0x08000000 if os.name == "nt" else 0  # 콘솔 창 안 띄우기
            proc = subprocess.run(
                [self.cli_path, "-p", "--model", self.cli_model],
                input=AI_PROMPT + self._hint_block(payload) + "\n\n이번 입력:\n" + payload, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=90, creationflags=flags)
            if proc.returncode != 0:
                dlog("AI-ERR", proc.returncode, proc.stderr[:300], proc.stdout[:300])
                raise RuntimeError((proc.stderr or proc.stdout or "claude 실행 실패").strip()[:120])
            dlog("AI-IN", payload.replace("\n", " / "))
            dlog("AI-OUT", proc.stdout.strip().replace("\n", " / "))
            return self._parse(proc.stdout, len(msgs))
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
        import subprocess
        flags = 0x08000000 if os.name == "nt" else 0
        proc = subprocess.run([ai.cli_path, "-p", "--model", ai.cli_model], input=prompt,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=90, creationflags=flags)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "claude 실행 실패").strip()[:120])
        out = proc.stdout.strip()
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
    status.pack(side="left", padx=8, pady=4)

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
            if ai.enabled:
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
    body.tag_configure("gap", font=("Malgun Gothic", 3))
    body.tag_configure("chan", foreground="#8f98ab", font=("Segoe UI", max(8, cfg["font_size"] - 2)))
    body.tag_configure("err", foreground="#ff7b7b")
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

    def render():
        body.configure(state="normal")
        body.delete("1.0", "end")
        for t in name_tags:
            body.tag_delete(t)
        name_tags.clear()
        hidden = set(cfg.get("hide_kinds") or [])
        view = view_var.get()
        allowed = VIEW_TABS.get(view)
        shown = [e for e in entries if not (e[4] and e[4] in hidden)
                 and (allowed is None or (not e[3] and (e[4] or "일반") in allowed))]
        for name, tr, orig, tag, kind, _eid, is_ai, ch in shown[-cfg["max_lines"]:]:
            if tag:
                body.insert("end", tr + "\n", tag)
                continue
            # 1줄: [종류] 닉네임
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
            body.insert("end", ("  ✦ " if is_ai else "  ▶ ") + tr + "\n", "msg")
            # 3줄: 원문 (선택)
            if cfg["show_original"] and orig and orig != tr:
                body.insert("end", "     " + orig + "\n", "orig")
            body.insert("end", "\n", "gap")
        body.configure(state="disabled")
        body.see("end")

    def info(msg, tag="info"):
        entries.append([None, msg, None, tag, None, 0, False, None])
        render()

    # --- 영역 선택 ---------------------------------------------------------
    def select_region():
        was_running = state["running"]
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
                           text="채팅 글자가 있는 부분만 드래그하세요  (취소: ESC)")
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
            if w < 40 or h < 15:
                info("영역이 너무 작아요. F9로 다시 선택하세요.", "err")
                return
            cfg["region"] = [left, top, w, h]
            save_config(cfg)
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

    def worker():
        while state["running"]:
            started = time.time()
            try:
                lines = ocr.read_lines(cfg["region"])
                batch = []  # (name, msg, protected, slots)
                for raw, rect, items in lines:
                    if not seen.is_new(raw):
                        continue
                    name, msg, ch = parse_line(raw)
                    if name and not msg.strip():
                        pending[0] = (name, ch, items)  # 닉네임만 읽힌 줄 -> 다음 줄이 대화
                        continue
                    if not name and pending[0]:
                        name, ch, p_items = pending[0]
                        items = list(p_items) + [i for i in items if i not in p_items]
                    pending[0] = None
                    msg = normalize_trade(merge_item_links(msg, items))
                    kind = classify_line(ocr.last_img, rect)
                    letters = len(re.findall(r"[A-Za-z가-힣0-9?!]", msg))
                    if letters < (1 if name else 3):
                        continue  # OCR 찌꺼기 (닉네임이 있는 줄은 "dd", "ty" 같은 짧은 말도 표시)
                    if name and cfg.get("my_name") and name.lower() == cfg["my_name"].lower():
                        out_q.put((name, msg, msg, None, kind, next_id(), ch))
                        continue
                    plain = re.sub(r"\[[^\]]*\]", "", msg)
                    if cfg["skip_korean_lines"] and plain.strip() and len(HANGUL_RE.findall(plain)) > len(plain) * 0.3:
                        out_q.put((name, msg, msg, None, kind, next_id(), ch))
                        continue
                    protected, slots = glossary.protect(msg)
                    batch.append((name, msg, protected, slots, kind, ch))
                if batch:
                    translated, err = translator.translate_many([b[2] for b in batch])
                    for (name, msg, _p, slots, kind, ch), tr in zip(batch, translated):
                        eid = next_id()
                        out_q.put((name, glossary.restore(tr, slots), msg, None, kind, eid, ch))
                        if ai.enabled:
                            ai_q.put((eid, msg))
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
        try:
            test = ai.translate_batch(["gz tyfp"])
            out_q.put((None, f"✦ AI 번역 연결 확인 완료 (테스트: gz tyfp → {test[0]})", None, "info", None, 0))
        except Exception as e:  # noqa: BLE001
            out_q.put((None, f"✦ AI 번역 연결 실패: {e}  → 구글 번역만 표시돼요", None, "err", None, 0))
        fails = 0
        while True:
            job = [ai_q.get()]
            time.sleep(0.4)  # 같이 올라온 줄 모아서 한 번에
            while not ai_q.empty() and len(job) < 15:
                job.append(ai_q.get_nowait())
            try:
                outs = ai.translate_batch([m for _i, m in job])
                for (eid, _m), tr in zip(job, outs):
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
        while True:
            try:
                item = out_q.get_nowait()
            except queue.Empty:
                break
            if item[0] == "__update__":
                for e in reversed(entries):
                    if e[5] == item[1]:
                        e[1], e[6] = item[2], True
                        break
            else:
                entries.append(list(item[:6]) + [False, item[6] if len(item) > 6 else None])
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
        start_btn.configure(text="■ 정지")
        status.configure(text="번역 중…  (F8 정지)", fg="#7ee08a")
        state["worker"] = threading.Thread(target=worker, daemon=True)
        state["worker"].start()

    def stop():
        state["running"] = False
        start_btn.configure(text="▶ 시작")
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

    def on_close():
        state["running"] = False
        cfg["overlay_geometry"] = root.geometry()
        save_config(cfg)
        root.destroy()

    btn("✕", on_close)
    btn("원문", toggle_orig)
    btn("사전 새로고침", reload_glossary)
    btn("영역(F9)", select_region)
    start_btn = btn("▶ 시작", toggle)

    root.bind("<F8>", lambda e: toggle())
    root.bind("<F9>", lambda e: select_region())
    root.protocol("WM_DELETE_WINDOW", on_close)

    # 게임 창에 포커스가 있어도 F8/F9가 먹도록 (가능할 때만)
    try:
        import keyboard
        keyboard.add_hotkey("f8", lambda: root.after(0, toggle))
        keyboard.add_hotkey("f9", lambda: root.after(0, select_region))
    except Exception:  # noqa: BLE001
        info("전역 단축키를 못 켰어요. 오버레이 버튼을 쓰거나 관리자 권한으로 실행하세요.", "err")

    set_view(view_var.get())
    info("성윤님 반가워요! 오버레이는 채팅창과 안 겹치는 곳에 두세요.")
    if ai.enabled:
        info("AI 번역 켜짐: ▶ 구글 번역이 먼저 뜨고, 잠시 뒤 ✦ 자연스러운 한국어로 바뀌어요.")
    elif ai.error:
        info("AI 번역 꺼짐: " + ai.error + " (구글 번역만 사용)", "err")
    if ocr_error:
        info("OCR 준비 실패: " + ocr_error, "err")
    elif cfg.get("region"):
        info("저장된 채팅창 영역이 있어요. F8로 바로 시작할 수 있어요.")

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
