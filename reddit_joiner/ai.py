#!/usr/bin/env python3
"""VADER sentiment + OmniRoute / Ollama (mistral:7b) / cloud API comments."""

from __future__ import annotations

import json
import os
import random
import re
from typing import Dict, List, Optional, Sequence, Tuple

import requests

try:
    from dotenv import load_dotenv

    from reddit_joiner.paths import ENV_FILE

    load_dotenv(ENV_FILE)
except ImportError:
    pass

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
except ImportError:
    SentimentIntensityAnalyzer = None  # type: ignore[misc, assignment]

_ANALYZER = None
AI_TIMEOUT = 22
AI_TIMEOUT_REASONING = 50
OLLAMA_TIMEOUT = 180
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "mistral:7b").strip() or "mistral:7b"
OMNIROUTE_HOST = os.environ.get("OMNIROUTE_HOST", "http://127.0.0.1:20128").rstrip("/")
OMNIROUTE_API_KEY = (
    os.environ.get("OMNIROUTE_API_KEY", "").strip()
    or os.environ.get("OMNIROUTE_KEY", "").strip()
    or "sk_omniroute"
)
OMNIROUTE_MODEL = os.environ.get("OMNIROUTE_MODEL", "").strip()


def _analyzer():
    global _ANALYZER
    if _ANALYZER is None and SentimentIntensityAnalyzer is not None:
        _ANALYZER = SentimentIntensityAnalyzer()
    return _ANALYZER


def sentiment_score(text: str) -> float:
    engine = _analyzer()
    if engine is None:
        return 0.0
    return float(engine.polarity_scores(text or "").get("compound") or 0.0)


def is_negative(text: str, threshold: float = 0.0) -> bool:
    return sentiment_score(text) < threshold


_STOP = {
    "this", "that", "with", "from", "your", "have", "what", "when", "where", "about",
    "just", "like", "they", "them", "been", "were", "will", "would", "could", "should",
    "into", "over", "also", "some", "more", "most", "than", "then", "best", "ever",
    "the", "and", "for", "you", "are", "not", "but", "how", "why", "who", "any",
    "can", "get", "got", "has", "had", "did", "does", "its", "it's", "dont", "don't",
}


def analyze_post(title: str, body: str, subreddit: str = "") -> Dict[str, object]:
    """Read the post and return mood, topic, intent, and keywords for a matching comment."""
    blob = f"{title or ''}\n{body or ''}"
    score = sentiment_score(blob)
    if score <= -0.25:
        mood = "negative"
    elif score >= 0.25:
        mood = "positive"
    else:
        mood = "neutral"
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z']{2,}", title or "")]
    keywords = [w for w in words if w not in _STOP][:8]
    topic = " ".join(keywords[:6]) or re.sub(r"\s+", " ", (title or "this").strip())[:70]
    intent = classify_post_intent(title, body)
    return {
        "sentiment": score,
        "mood": mood,
        "intent": intent,
        "keywords": keywords,
        "topic": topic,
        "subreddit": (subreddit or "").strip(),
        "title": (title or "").strip(),
        "excerpt": re.sub(r"\s+", " ", (body or "").strip())[:280],
    }


_HELP_PATTERNS = (
    r"\bhelp\b",
    r"\badvice\b",
    r"\bneed (?:help|advice|opinions?)\b",
    r"\bshould i\b",
    r"\bwhat (?:should|do|can) i\b",
    r"\bhow (?:do|can|should) i\b",
    r"\bplease help\b",
    r"\bstuck\b",
    r"\bconfused\b",
    r"\bwhat would you (?:do|say)\b",
    r"\bstruggling\b",
    r"\bhaving (?:trouble|issues?|problems?)\b",
    r"\b(?:trouble|issue|problem) with\b",
    r"\bnot working\b",
    r"\bhow (?:to|do i) fix\b",
    r"\bcan'?t (?:figure out|get it to|seem to)\b",
    r"\bany (?:help|advice)\b",
)
_SUGGEST_PATTERNS = (
    r"\brecommend",
    r"\bsuggest",
    r"\bany (?:tips?|ideas?|suggestions?)\b",
    r"\blooking for\b",
    r"\bwhat(?:'s| is) (?:a )?good\b",
    r"\bbest (?:way|option|tool|app|book)\b",
    r"\bwhich (?:one|should)\b",
    r"\balternatives?\b",
    r"\bhelp me (?:choose|pick|decide)\b",
    r"\bwhich (?:is|would be) better\b",
    r"\bwhere should i\b",
    r"\bwhat would you (?:pick|choose|get|buy)\b",
)
_REVIEW_PATTERNS = (
    r"\breview\b",
    r"\bthoughts on\b",
    r"\bworth (?:it|buying|getting|trying)\b",
    r"\bhas anyone (?:tried|used|bought)\b",
    r"\bopinions? on\b",
    r"\bhow (?:is|was|good is)\b",
    r"\bfirst impressions?\b",
    r"\brating\b",
    r"\bfeedback on\b",
    # Common ways Reddit actually asks for a verdict. Needed because the intent
    # gate is strict now: a missed label means the post gets no comment at all.
    r"\b(?:rate|roast|critique|judge) my\b",
    r"\bhow does (?:this|it|mine|my) look\b",
    r"\bdoes this look\b",
    r"\bam i doing (?:this|it) right\b",
    r"\bany feedback\b",
    r"\bwhat do you (?:think|guys think) (?:of|about)\b",
    r"\bgood (?:deal|buy|choice|value)\b",
)
_QUESTION_PATTERNS = (
    r"\banyone know\b",
    r"\bdoes anyone\b",
    r"\bcan anyone\b",
    r"\bwho (?:has|knows|else)\b",
    r"\bis (?:there|it|this|that)\b",
    r"\bwhy (?:do|does|is|are|did|would|can't|cant)\b",
    r"\bwhat (?:is|are|was|were|if|about)\b",
    r"\bwhere (?:can|do|should|is)\b",
)

COMMENTABLE_INTENTS = frozenset({"help", "suggestion", "review", "question"})


def classify_post_intent(title: str, body: str = "") -> str:
    """Soft label: help | suggestion | review | question | other."""
    title_l = (title or "").lower()
    blob = f"{title_l}\n{(body or '')[:500]}".lower()
    scores = {
        "help": sum(1 for pat in _HELP_PATTERNS if re.search(pat, blob)),
        "suggestion": sum(1 for pat in _SUGGEST_PATTERNS if re.search(pat, blob)),
        "review": sum(1 for pat in _REVIEW_PATTERNS if re.search(pat, blob)),
        "question": sum(1 for pat in _QUESTION_PATTERNS if re.search(pat, blob)),
    }
    if "?" in (title or ""):
        scores["question"] += 1
    best = max(scores.items(), key=lambda item: item[1])
    if best[1] <= 0:
        return "other"
    top = best[1]
    tied = [name for name, value in scores.items() if value == top]
    # Prefer help wording over a bare "?" when both score equally
    if "help" in tied and "question" in tied and scores["help"] >= 1:
        return "help"
    return random.choice(tied)


def is_commentable_intent(intent: str) -> bool:
    return str(intent or "").strip().lower() in COMMENTABLE_INTENTS


def local_comment_for_post(analysis: Dict[str, object]) -> str:
    """Fallback comment that answers this post, not a canned one-liner."""
    title = re.sub(r"\s+", " ", str(analysis.get("title") or "")).strip()
    words = title.split()
    detail = " ".join(words[:8]).strip(" ?.!")
    if len(detail) > 60:
        detail = detail[:57].rstrip() + "..."
    if len(detail) < 8:
        detail = str(analysis.get("topic") or "what you described").strip()
    intent = str(analysis.get("intent") or "other")
    if intent == "help":
        options = [
            f"The {detail} part is the one I'd deal with first. What's the main thing you've already tried?",
            f"I've been stuck on something like {detail} too. One small change is usually enough to see if you're on the right track.",
            f"For {detail}, I'd start with the simplest fix and only change one thing. Happy to narrow it if you say what failed.",
        ]
    elif intent == "suggestion":
        options = [
            f"If I were choosing for {detail}, I'd take the option you can undo easily and live with it for a week.",
            f"On {detail}, the lower-commitment option is the one I'd try first. Which limit matters more for you, time or money?",
            f"For {detail} I'd skip the fancy version until you know you'll actually use it. What are you leaning toward?",
        ]
    elif intent == "review":
        options = [
            f"The bit about {detail} is what I'd want more of. Was there one thing that was better or worse than you expected?",
            f"Useful write-up on {detail}. Would you pick it again, or is there something you'd change?",
            f"The everyday detail on {detail} is the useful part. How long have you actually been using it?",
        ]
    elif intent == "question":
        options = [
            f"On {detail}, I'd start from what you already have and only add something if it clearly fills a gap. What's the must-have for you?",
            f"Short version for {detail}: keep it simple and compare with someone in the same spot. What constraint is blocking you?",
            f"For {detail} the answer usually depends on budget versus how much hassle you'll tolerate. Which one is tighter?",
        ]
    else:
        options = [
            f"The {detail} part is what I actually wanted to reply to. Curious how that played out for you.",
        ]
    return _clean_comment(random.choice(options))


def _clean_comment(text: str) -> str:
    value = _strip_think(text or "").strip().strip('"').strip("'")
    value = _strip_fences(value)
    value = re.sub(r"(?is)^(?:sure|okay|ok|here(?:'s| is)|comment:)\s*[:\-]*\s*", "", value)
    value = re.sub(r"\s+", " ", value)
    parts = re.split(r"(?<=[.!?])\s+", value)
    if len(parts) > 2:
        value = " ".join(parts[:2]).strip()
    value = value[:300].strip()
    letters = len(re.findall(r"[A-Za-z]", value))
    if letters < 18:
        return ""
    # Checked last, so it also catches tells revealed by the trimming above.
    if looks_like_automation(value):
        return ""
    return value


def _strip_fences(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _parse_json_object(text: str) -> Dict[str, str]:
    raw = _strip_fences(text)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): str(v) if v is not None else "" for k, v in data.items()}
    except Exception:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return {str(k): str(v) if v is not None else "" for k, v in data.items()}
        except Exception:
            pass
    return {}


_THINK_TAGS = ("think", "thinking", "reasoning", "scratchpad", "analysis")

# Phrases that mean the text is the model talking about itself or refusing,
# rather than a Reddit comment. Posting any of these outs the account instantly,
# so they are treated as a generation failure, not something to clean up.
_AUTOMATION_TELLS = (
    # Self-disclosure. Kept narrow so ordinary sentences do not trip it.
    r"\bas an? (?:ai|a\.i\.|language model|assistant)\b",
    r"\b(?:i'?m|i am) (?:an? )?(?:ai|a\.i\.|language model|chatbot|large language)\b",
    r"\b(?:i'?m|i am) (?:just )?(?:a|an) (?:bot|automated)\b",
    r"\bmy (?:training data|guidelines|programming|knowledge cutoff|system prompt)\b",
    r"\b(?:i'?m|i am) (?:powered by|built by|trained by)\b",
    r"\b(?:as|i'?m) (?:chatgpt|claude|gemini|deepseek|gpt-?\d)\b",
    # Refusals. A refusal object is required, so "I can't tell from the photo"
    # and "I cannot recommend it enough" stay allowed.
    r"\bi (?:cannot|can'?t|am unable to|won'?t be able to) "
    r"(?:assist|help you with that|provide|comply|generate|create|write|fulfill|answer that)\b",
    r"\bi'?m (?:sorry|afraid),? but i (?:cannot|can'?t|am unable)\b",
    r"\bi must (?:decline|refuse)\b",
    r"\b(?:against|violates) my (?:guidelines|policies|programming)\b",
    # Prompt and scaffolding leakage.
    r"\breturn only json\b",
    r"\bwriter context\b",
    r"\bage_days\s*=|\bkarma\s*=",
    r"\bhere(?:'s| is) (?:the|a|your) (?:comment|post|reply|response) (?:you|i|for|as)\b",
    r"\bcertainly!\s*here\b",
    r"\{\s*[\"']?(?:title|body|comment)[\"']?\s*:",
    r"\[(?:insert|your|topic|subreddit)[^\]]{0,30}\]",
    r"\{\{?\s*(?:topic|title|subreddit|body|hook)\s*\}?\}",
    r"^\s*(?:comment|reply|response)\s*:\s",
    r"```",
)
_AUTOMATION_RE = re.compile("|".join(_AUTOMATION_TELLS), re.I)


def _strip_think(text: str) -> str:
    value = text or ""
    for tag in _THINK_TAGS:
        value = re.sub(rf"<{tag}>[\s\S]*?</{tag}>", " ", value, flags=re.I)
        # Unclosed reasoning block: everything after the opening tag is monologue.
        value = re.sub(rf"<{tag}>[\s\S]*$", " ", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def looks_like_automation(text: str) -> bool:
    """True when this text would expose the account if posted."""
    return bool(_AUTOMATION_RE.search(str(text or "")))


def _message_text(payload: Dict[str, object]) -> str:
    choice = ((payload.get("choices") or [{}])[0] if isinstance(payload, dict) else {}) or {}
    message = (choice.get("message") or {}) if isinstance(choice, dict) else {}
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        content = "".join(parts)
    # `reasoning_content` is the model's private monologue, which reasoning
    # models return alongside an empty `content`. It must never become a
    # comment: an empty content field is a generation failure, so report it as
    # empty and let the caller fall back or abandon.
    return _strip_think(str(content or ""))


def _openai_compat_text(
    *,
    url: str,
    api_key: str,
    model: str,
    prompt: str,
    system: str,
    max_tokens: int,
    timeout: float,
    extra_headers: Optional[Dict[str, str]] = None,
) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    response = requests.post(
        url,
        headers=headers,
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.85,
            "max_tokens": max_tokens,
        },
        timeout=timeout,
    )
    if response.status_code in {400, 404}:
        raise RuntimeError(f"{model} not available ({response.status_code})")
    if response.status_code in {401, 403}:
        raise RuntimeError(f"auth failed ({response.status_code})")
    response.raise_for_status()
    return _message_text(response.json())


def _api_keys() -> Dict[str, str]:
    return {
        "huggingface": (
            os.environ.get("HF_TOKEN", "").strip()
            or os.environ.get("HUGGINGFACE_HUB_TOKEN", "").strip()
            or os.environ.get("HUGGINGFACE_API_KEY", "").strip()
        ),
        "deepseek": os.environ.get("DEEPSEEK_API_KEY", "").strip(),
        "openrouter": os.environ.get("OPENROUTER_API_KEY", "").strip(),
        "groq": os.environ.get("GROQ_API_KEY", "").strip(),
        "openai": os.environ.get("OPENAI_API_KEY", "").strip(),
        "gemini": (
            os.environ.get("GEMINI_API_KEY", "").strip()
            or os.environ.get("GOOGLE_API_KEY", "").strip()
        ),
    }


def _huggingface_text(prompt: str, system: str, api_key: str, max_tokens: int) -> str:
    """Free Hugging Face Inference Providers (OpenAI-compatible router)."""
    configured = (
        os.environ.get("HF_MODEL", "").strip()
        or os.environ.get("HUGGINGFACE_MODEL", "").strip()
    )
    models = [
        configured,
        "HuggingFaceTB/SmolLM3-3B:fastest",
        "HuggingFaceTB/SmolLM3-3B",
        "Qwen/Qwen2.5-3B-Instruct:cheapest",
        "Qwen/Qwen2.5-3B-Instruct",
        "Qwen/Qwen2.5-1.5B-Instruct",
        "microsoft/Phi-3.5-mini-instruct",
    ]
    last_error: Optional[BaseException] = None
    seen = set()
    for model in models:
        if not model or model in seen:
            continue
        seen.add(model)
        try:
            return _openai_compat_text(
                url="https://router.huggingface.co/v1/chat/completions",
                api_key=api_key,
                model=model,
                prompt=prompt,
                system=system,
                max_tokens=max_tokens,
                timeout=max(AI_TIMEOUT, 40),
            )
        except Exception as exc:
            last_error = exc
            continue
    raise last_error or RuntimeError("Hugging Face request failed")


def _deepseek_text(prompt: str, system: str, api_key: str, max_tokens: int) -> str:
    configured = os.environ.get("DEEPSEEK_MODEL", "").strip()
    models = [
        configured,
        "deepseek-v4-flash",
        "deepseek-chat",
        "deepseek-reasoner",
    ]
    last_error: Optional[BaseException] = None
    seen = set()
    for model in models:
        if not model or model in seen:
            continue
        seen.add(model)
        timeout = AI_TIMEOUT_REASONING if "reasoner" in model or "r1" in model else AI_TIMEOUT
        try:
            return _openai_compat_text(
                url="https://api.deepseek.com/v1/chat/completions",
                api_key=api_key,
                model=model,
                prompt=prompt,
                system=system,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        except Exception as exc:
            last_error = exc
            continue
    raise last_error or RuntimeError("DeepSeek request failed")


def _openrouter_text(prompt: str, system: str, api_key: str, max_tokens: int) -> str:
    configured = os.environ.get("OPENROUTER_MODEL", "").strip()
    models = [
        configured,
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-chat",
        "deepseek/deepseek-r1",
        "openrouter/free",
        "z-ai/glm-5.2:free",
        "google/gemma-4-26b-a4b-it:free",
        "minimax/minimax-m2.7:free",
    ]
    headers = {
        "HTTP-Referer": "https://github.com/local/reddit-joiner",
        "X-Title": "reddit-joiner",
    }
    last_error: Optional[BaseException] = None
    seen = set()
    for model in models:
        if not model or model in seen:
            continue
        seen.add(model)
        timeout = AI_TIMEOUT_REASONING if "r1" in model.lower() or "reason" in model.lower() else AI_TIMEOUT
        try:
            return _openai_compat_text(
                url="https://openrouter.ai/api/v1/chat/completions",
                api_key=api_key,
                model=model,
                prompt=prompt,
                system=system,
                max_tokens=max_tokens,
                timeout=timeout,
                extra_headers=headers,
            )
        except Exception as exc:
            last_error = exc
            continue
    raise last_error or RuntimeError("OpenRouter request failed")


def _groq_text(prompt: str, system: str, api_key: str, max_tokens: int) -> str:
    configured = os.environ.get("GROQ_MODEL", "").strip()
    models = [
        configured,
        "llama-3.1-8b-instant",
        "openai/gpt-oss-20b",
        "llama-3.3-70b-versatile",
    ]
    last_error: Optional[BaseException] = None
    seen = set()
    for model in models:
        if not model or model in seen:
            continue
        seen.add(model)
        try:
            return _openai_compat_text(
                url="https://api.groq.com/openai/v1/chat/completions",
                api_key=api_key,
                model=model,
                prompt=prompt,
                system=system,
                max_tokens=max_tokens,
                timeout=AI_TIMEOUT,
            )
        except Exception as exc:
            last_error = exc
            continue
    raise last_error or RuntimeError("Groq request failed")


def _openai_text(prompt: str, system: str, api_key: str, max_tokens: int) -> str:
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    return _openai_compat_text(
        url="https://api.openai.com/v1/chat/completions",
        api_key=api_key,
        model=model,
        prompt=prompt,
        system=system,
        max_tokens=max_tokens,
        timeout=AI_TIMEOUT,
    )


def _scrub_secret(text: str) -> str:
    return re.sub(r"([?&]key=)[^&\s]+", r"\1***", str(text or ""), flags=re.I)


def _gemini_text(prompt: str, system: str, api_key: str, max_tokens: int) -> str:
    configured = os.environ.get("GEMINI_MODEL", "").strip()
    models = [
        configured,
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-flash-latest",
        "gemini-1.5-flash",
        "gemini-2.0-flash",
    ]
    seen = set()
    last_error: Optional[BaseException] = None
    full = f"{system}\n\n{prompt}" if system else prompt
    for model in models:
        if not model or model in seen:
            continue
        seen.add(model)
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        response = requests.post(
            url,
            json={
                "contents": [{"parts": [{"text": full}]}],
                "generationConfig": {"temperature": 0.85, "maxOutputTokens": max_tokens},
            },
            timeout=AI_TIMEOUT,
        )
        if response.status_code in {404, 429, 500, 503}:
            last_error = RuntimeError(f"gemini {model}: HTTP {response.status_code}")
            continue
        response.raise_for_status()
        parts = (((response.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
        return "".join(str(part.get("text") or "") for part in parts)
    if last_error:
        raise last_error
    raise RuntimeError("No Gemini model available")


def _omniroute_catalog() -> list:
    try:
        response = requests.get(
            f"{OMNIROUTE_HOST}/v1/models",
            headers={"Authorization": f"Bearer {OMNIROUTE_API_KEY}"},
            timeout=8,
        )
        if response.status_code >= 400:
            return []
        rows = (response.json() or {}).get("data") or []
        return [str((row or {}).get("id") or "") for row in rows if (row or {}).get("id")]
    except Exception:
        return []


def omniroute_ready() -> bool:
    """True if OmniRoute is up and local Ollama mistral is in the catalog."""
    try:
        health = requests.get(f"{OMNIROUTE_HOST}/healthz", timeout=3)
        if not health.ok:
            return False
        return any(
            ("ollama/" in item or "ollama-local/" in item) and "mistral" in item
            for item in _omniroute_catalog()
        )
    except Exception:
        return False


def omniroute_base() -> str:
    return OMNIROUTE_HOST


def _omniroute_text(prompt: str, system: str, api_key: str, max_tokens: int) -> str:
    """OpenAI-compatible chat via diegosouzapw/OmniRoute (http://localhost:20128/v1)."""
    if not omniroute_ready():
        raise RuntimeError("OmniRoute is not running at " + OMNIROUTE_HOST)
    catalog = _omniroute_catalog()
    models = [
        OMNIROUTE_MODEL,
        f"ollama/{OLLAMA_MODEL}",
        f"ollama-local/{OLLAMA_MODEL}",
        OLLAMA_MODEL,
        "ollama/mistral:7b",
        "ollama-local/mistral:7b",
    ]
    models.extend(
        item
        for item in catalog
        if ("ollama/" in item or "ollama-local/" in item) and "mistral" in item
    )
    last_error: Optional[BaseException] = None
    seen = set()
    for model in models:
        if not model or model in seen:
            continue
        seen.add(model)
        try:
            return _openai_compat_text(
                url=f"{OMNIROUTE_HOST}/v1/chat/completions",
                api_key=api_key or OMNIROUTE_API_KEY,
                model=model,
                prompt=prompt,
                system=system,
                max_tokens=max_tokens,
                timeout=OLLAMA_TIMEOUT,
            )
        except Exception as exc:
            last_error = exc
            text = str(exc).lower()
            if "auth failed" in text or "401" in text or "403" in text:
                break
            continue
    raise last_error or RuntimeError("OmniRoute request failed")


def ollama_ready() -> bool:
    """True if the local Ollama server answers."""
    try:
        response = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=3)
        return response.ok
    except Exception:
        return False


def ollama_model_name() -> str:
    return OLLAMA_MODEL


def _ollama_has_model(model: str) -> bool:
    try:
        response = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        response.raise_for_status()
        rows = (response.json() or {}).get("models") or []
        wanted = (model or "").strip().lower()
        short = wanted.split(":")[0]
        for row in rows:
            name = str((row or {}).get("name") or "").strip().lower()
            if name == wanted or name.startswith(wanted) or name.split(":")[0] == short:
                return True
    except Exception:
        return False
    return False


def _ollama_text(prompt: str, system: str, api_key: str, max_tokens: int) -> str:
    """Local Ollama chat. Default model is mistral:7b."""
    model = ollama_model_name()
    if not ollama_ready():
        raise RuntimeError("Ollama is not running at " + OLLAMA_HOST)
    if not _ollama_has_model(model):
        raise RuntimeError(f"Ollama model {model} is not pulled — run: ollama pull {model}")
    response = requests.post(
        f"{OLLAMA_HOST}/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "keep_alive": "30m",
            "options": {
                "temperature": 0.85,
                "num_predict": max(80, int(max_tokens or 160)),
            },
        },
        timeout=OLLAMA_TIMEOUT,
    )
    if response.status_code == 404:
        raise RuntimeError(f"Ollama model {model} not found")
    response.raise_for_status()
    payload = response.json()
    text = str(((payload.get("message") or {}).get("content") or "")).strip()
    if not text:
        text = str(payload.get("response") or "").strip()
    if not text:
        raise RuntimeError("Ollama returned an empty comment")
    return text


def _llm_text(
    prompt: str,
    system: str,
    max_tokens: int = 180,
    *,
    fast: bool = False,
    prefer: str = "",
) -> Tuple[str, str]:
    """Try OmniRoute first, then local Ollama (mistral:7b), then cloud APIs.

    `prefer="openai"` is the comment path: that key is used before local models.
    """
    keys = _api_keys()
    errors = []
    if prefer == "openai" and keys.get("openai"):
        try:
            return _openai_text(prompt, system, keys["openai"], max_tokens), "openai"
        except Exception as exc:
            errors.append(f"openai: {_scrub_secret(str(exc))}")
    if omniroute_ready():
        try:
            return _omniroute_text(prompt, system, OMNIROUTE_API_KEY, max_tokens), "omniroute"
        except Exception as exc:
            errors.append(f"omniroute: {_scrub_secret(str(exc))}")
    if ollama_ready():
        try:
            return _ollama_text(prompt, system, "", max_tokens), f"ollama:{ollama_model_name()}"
        except Exception as exc:
            errors.append(f"ollama: {_scrub_secret(str(exc))}")
    tries = [
        ("huggingface", keys.get("huggingface") or "", _huggingface_text),
        ("deepseek", keys.get("deepseek") or "", _deepseek_text),
        ("openrouter", keys.get("openrouter") or "", _openrouter_text),
        ("groq", keys.get("groq") or "", _groq_text),
        ("gemini", keys.get("gemini") or "", None),
        ("openai", keys.get("openai") or "", _openai_text),
    ]
    if fast:
        tries = [
            item
            for item in tries
            if item[0] in {"huggingface", "deepseek", "openrouter", "gemini", "groq"}
        ]
    for name, key, fn in tries:
        if not key:
            continue
        try:
            if name == "gemini":
                return _gemini_text(prompt, system, key, max_tokens), "gemini"
            return fn(prompt, system, key, max_tokens), name  # type: ignore[misc]
        except Exception as exc:
            errors.append(f"{name}: {_scrub_secret(str(exc))}")
    if not omniroute_ready() and not ollama_ready() and not any(keys.values()):
        raise RuntimeError(
            "OmniRoute is not running, "
            "Ollama is down, and no cloud API key is set"
        )
    raise RuntimeError("; ".join(errors) or "AI request failed")


def _comment_uses_post(comment: str, title: str, body: str) -> bool:
    """True when the reply names something from this post, not a generic line."""
    words = [
        word.lower()
        for word in re.findall(r"[A-Za-z][A-Za-z']{4,}", f"{title}\n{(body or '')[:500]}")
        if word.lower() not in _STOP
    ]
    if not words:
        return True
    blob = (comment or "").lower()
    return any(word in blob for word in words[:12])


def generate_ai_comment(
    title: str,
    body: str,
    subreddit: str = "",
    tone: str = "friendly",
    analysis: Optional[Dict[str, object]] = None,
    rules: str = "",
) -> Tuple[str, str]:
    """
    Return (comment, provider). Raises RuntimeError if no provider is configured
    or every provider fails.
    """
    info = analysis or analyze_post(title, body, subreddit)
    mood = str(info.get("mood") or "neutral")
    intent = str(info.get("intent") or classify_post_intent(title, body))
    if not is_commentable_intent(intent):
        raise RuntimeError(
            "post is not help, suggestion, review, or question"
        )
    place = f"r/{subreddit.strip()}" if (subreddit or "").strip() else "this Reddit community"
    style = (tone or "friendly").strip().lower()
    if style == "funny":
        style = "friendly"
    if mood == "negative":
        style = "neutral"
    tone_line = {
        "expert": "Sound like a knowledgeable hobbyist: specific and humble, not lecturing.",
        "funny": "Light dry humor is ok. No memes, no sarcasm that could look mean.",
        "neutral": "Keep it plain and concise.",
        "friendly": "Be warm and conversational, like a regular on the sub.",
    }.get(style, "Be warm and conversational, like a regular on the sub.")
    mood_line = {
        "negative": "The post is frustrated or unhappy. Be careful and empathetic. Do not cheerlead or say the post is great.",
        "positive": "The post is upbeat. You can agree, but still mention something specific.",
        "neutral": "Stay on the actual topic. Do not write a generic 'nice post' line.",
    }.get(mood, "Stay on the actual topic.")
    intent_line = {
        "help": (
            "They need help. Give one practical next step from their actual situation, "
            "then ask one short question so they can reply. Sound like you have dealt with this, not like a guide."
        ),
        "suggestion": (
            "They want ideas. Recommend one specific option that fits what they wrote, "
            "and ask which limit matters more so the thread keeps going."
        ),
        "review": (
            "This is a review or 'is it worth it' post. React to one detail they mentioned "
            "and ask whether they would choose it again or what surprised them."
        ),
        "question": (
            "Answer their actual question in plain words. Then ask one follow-up that a person "
            "in the thread would actually answer."
        ),
    }.get(intent, "Reply to what they actually said.")
    rule_blob = re.sub(r"\s+", " ", (rules or "").strip())[:900]
    rule_line = (
        f" Follow these community rules. Do not break them: {rule_blob}"
        if rule_blob
        else ""
    )
    prompt = (
        f"Write one Reddit comment (1-2 sentences) for {place}. "
        f"{tone_line} {mood_line} {intent_line}{rule_line} "
        "Write the way a person types on their phone: contractions, a little uneven, no essay. "
        "Use a real detail from the title or body (a product, place, problem, or choice). "
        "Do not say great post, thanks for sharing, nice write-up, or hope this helps. "
        "Do not list keywords. Do not start with Yeah, Absolutely, or Nice take. "
        "No hashtags, no quotes around the whole comment, no username, no asking for upvotes.\n\n"
        f"Title: {title}\n\n{(body or '')[:1200]}"
    )
    system = (
        "You write short Reddit comments that answer the post and invite a reply. "
        "Only comment as if this is a help request, a suggestion thread, a review, or a question. "
        "Follow the community rules if they were given. "
        "Never mention being a bot, karma, or that the account is new."
    )
    text, provider = _llm_text(prompt, system, max_tokens=160, fast=True, prefer="openai")
    cleaned = _clean_comment(text)
    if cleaned and not _comment_uses_post(cleaned, title, body):
        retry = (
            prompt
            + "\n\nYour comment must name a specific detail from the title or body. "
            "Do not write a generic reply."
        )
        text, provider = _llm_text(retry, system, max_tokens=160, fast=True, prefer="openai")
        cleaned = _clean_comment(text)
    if not cleaned:
        # The old loose path posted raw model output whenever cleaning came back
        # empty, skipping every filter above — which is exactly how a refusal or
        # a reasoning dump reaches Reddit. Salvage the first sentences instead,
        # and hold them to the same checks.
        loose = _strip_fences(_strip_think(text or "")).strip().strip('"')
        loose = " ".join(re.split(r"(?<=[.!?])\s+", loose)[:2])[:280].strip()
        if len(re.findall(r"[A-Za-z]", loose)) >= 18 and not looks_like_automation(loose):
            cleaned = loose
    if not cleaned:
        raise RuntimeError("AI comment was empty or looked automated")
    return cleaned, provider


def generate_ai_post(
    subreddit: str,
    *,
    description: str = "",
    rules: str = "",
    topics: Optional[Sequence[str]] = None,
    questions_only: bool = False,
    no_humor: bool = False,
    no_promo: bool = False,
    karma: int = 0,
    age_days: float = 0.0,
) -> Tuple[str, str, str]:
    """
    Return (title, body, provider) for a genuine text post that fits the subreddit.
    Uses recent discussion so the draft continues what people are talking about now.
    """
    name = (subreddit or "").strip().lstrip("r/")
    recent = [re.sub(r"\s+", " ", str(item or "")).strip() for item in (topics or [])]
    recent = [item for item in recent if 8 <= len(item) <= 280][:8]
    topic_block = "\n".join(f"- {item}" for item in recent) or "- (none listed)"
    shape = "Write a genuine question other members would answer."
    if questions_only:
        shape = "This community is for questions. The title MUST be a question."
    elif name.lower() in {"nostupidquestions", "askuk", "tooafraidtoask"}:
        shape = "Write a genuine question other members would answer. Title must be a question."
    elif name.lower() in {"advice", "internetparents", "careerguidance"}:
        shape = "Write a short advice-seeking post about an everyday situation."
    elif name.lower() in {"casualconversation", "newtoreddit"}:
        shape = "Write a light conversation starter, not a rant and not promo."
    extra = []
    if no_humor:
        extra.append("No jokes, memes, or sarcasm.")
    if no_promo:
        extra.append("No self-promo, brands, or links.")
    extra_line = (" ".join(extra) + "\n") if extra else ""
    newbie = (
        "This is a new low-karma account. Keep the post simple, on-topic, and humble. "
        "No slang about being new.\n"
        if karma <= 2
        else ""
    )
    prompt = (
        f"Write one original Reddit text post for r/{name}.\n"
        f"{shape}\n"
        "You just spent time scrolling this subreddit's New feed and reading a thread.\n"
        "Your post should continue the RECENT DISCUSSION below — same kind of topic, "
        "your own angle. Do not copy a title or body.\n"
        "Sound like a real person typing on their phone: first person, contractions, "
        "a bit messy is fine. Not an essay, not marketing, not a numbered list, "
        "not 'I wanted to share'.\n"
        "Follow the community rules below. If a rule forbids something, do not do it.\n"
        "Do not mention karma, being new, or upvotes. No links, no hashtags, no emoji spam.\n"
        f"{extra_line}"
        f"Public description: {(description or 'n/a')[:700]}\n"
        f"Community rules (obey these):\n{(rules or 'n/a')[:1500]}\n"
        f"Recent discussion on New (join this conversation, do not copy):\n{topic_block}\n"
        f"Writer context (do not mention this): karma={int(karma)}, age_days={age_days:.0f}.\n"
        f"{newbie}"
        'Return ONLY JSON: {"title": "...", "body": "..."}\n'
        "Title under 120 characters, like a human typed it. Body 2-5 short sentences."
    )
    system = (
        f"You write one natural Reddit post for r/{name} after reading today's threads. "
        "Human tone. Follow the rules. Continue the recent discussion without copying it."
    )
    text, provider = _llm_text(prompt, system, max_tokens=320)
    data = _parse_json_object(text)
    title = re.sub(r"\s+", " ", (data.get("title") or "")).strip().strip('"')[:300]
    body = _strip_think(data.get("body") or "").strip().strip('"')
    if not title:
        # First-line salvage when the model ignored the JSON instruction. Only
        # usable if it reads like a title rather than the model's preamble.
        line = _strip_fences(_strip_think(text)).splitlines()[0].strip().strip('"') if text else ""
        if line and not looks_like_automation(line):
            title = line[:120]
    if looks_like_automation(title):
        title = ""
    if looks_like_automation(body):
        body = ""
    if not title or not body:
        # Fall back wholesale rather than pairing a salvaged title with a canned
        # body, which used to let a prompt-leak title through on its own.
        local_title, local_body = _local_community_post(name, recent, questions_only)
        title, body = local_title, local_body
        provider = "local"
    if not title:
        raise RuntimeError("AI post title was empty")
    if questions_only and "?" not in title:
        # Appending "?" to a statement produced titles like "Here is my post?".
        # A question-only community needs a real question, so use the local
        # question template instead of punctuating a statement.
        local_title, local_body = _local_community_post(name, recent, True)
        if "?" in local_title:
            title, body, provider = local_title, local_body, "local"
        else:
            raise RuntimeError("could not build a question title for this community")
    body = body[:4000]
    return title, body, provider


def _local_community_post(
    subreddit: str,
    topics: Sequence[str],
    questions_only: bool,
) -> Tuple[str, str]:
    """Offline draft when the model is down — still shaped like today's discussion."""
    name = (subreddit or "here").strip()
    seed = ""
    for item in topics:
        bit = str(item).split(" — ", 1)[0].split(" (", 1)[0].strip()
        if 12 <= len(bit) <= 90:
            seed = bit.rstrip("?")
            break
    blob = " ".join(topics).lower()
    if questions_only or name.lower() in {"nostupidquestions", "askreddit", "askuk", "tooafraidtoask"}:
        if seed:
            title = random.choice(
                (
                    "Is it just me or is this a thing more than people admit?",
                    "What's the normal take on this? I keep seeing mixed answers",
                    "Am I overthinking this or is it fair to ask?",
                )
            )
            body = (
                "Been reading a few posts here and this keeps coming up. "
                "Not trying to restart the same thread, just stuck on it. "
                "Curious how people here actually handle it."
            )
        else:
            title = random.choice(
                (
                    "What's a normal thing in daily life that still puzzles you?",
                    "What's something people assume everyone knows, but you only learned later?",
                    "What small habit do you have that other people find odd?",
                )
            )
            body = (
                "Not looking for a debate. Just curious what others have noticed. "
                "Everyday examples are fine."
            )
        return title, body
    if "advice" in name.lower() or "should i" in blob:
        title = "Anyone else overthink a simple decision like this?"
        body = (
            "I keep second-guessing a small everyday choice after reading a few posts here. "
            "If you have been in the same spot, how did you settle it?"
        )
        return title, body
    title = random.choice(
        (
            "What's something from this week you keep thinking about?",
            "Anyone else notice this more than they used to?",
            "What's a small thing that made your day easier recently?",
        )
    )
    body = (
        "Just a thought after scrolling New for a bit. "
        "Curious if other people here see it the same way."
    )
    return title, body
