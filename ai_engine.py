import json
import re
import urllib.parse
from typing import Dict, Any, List, Optional
import requests

from database import get_settings, add_log


def _normalize_gemini_model(model_name: str) -> str:
    """Đảm bảo luôn sử dụng tên model Gemini hợp lệ trên Google Generative Language API."""
    m = (model_name or "").strip().lower()
    if not m or "3.8" in m or "3 flash" in m or "preview" in m:
        return "gemini-2.5-flash"
    return model_name.strip()


def translate_google_free(text: str, target_lang: str = "vi") -> str:
    """Dịch nhanh miễn phí qua Google Translate endpoint (giống class VrpOxq của DHB)."""
    if not text or not text.strip():
        return text
    lang_map = {
        "tiếng việt": "vi",
        "vietnamese": "vi",
        "vi": "vi",
        "tiếng anh": "en",
        "english": "en",
        "en": "en",
        "tiếng trung": "zh-CN",
        "chinese": "zh-CN",
        "tiếng nhật": "ja",
        "japanese": "ja",
        "tiếng hàn": "ko",
        "korean": "ko",
        "tiếng tây ban nha": "es",
        "spanish": "es",
    }
    tl = lang_map.get(target_lang.strip().lower(), target_lang.strip())
    url = (
        "https://translate.googleapis.com/translate_a/single"
        f"?client=gtx&sl=auto&tl={urllib.parse.quote(tl)}&dt=t&q={urllib.parse.quote(text)}"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data and isinstance(data[0], list):
            return "".join(part[0] for part in data[0] if part and part[0])
    except Exception as e:
        add_log("AI", f"Lỗi Google Translate: {e}", "WARNING")
    return text


def call_llm(
    prompt: str,
    provider: str = "gemini",
    system_instruction: str = "Bạn là chuyên gia tối ưu SEO YouTube & YouTube Shorts.",
) -> str:
    """
    Gọi AI Provider (Gemini / OpenAI GPT / DeepSeek / Google Translate).
    """
    settings = get_settings()
    prov = (provider or "gemini").strip().lower()

    if prov == "google_translate":
        return translate_google_free(prompt, "vi")

    if prov == "gemini":
        api_key = settings.get("gemini_api_key", "").strip()
        model = _normalize_gemini_model(settings.get("gemini_model", "gemini-2.5-flash"))
        if not api_key:
            raise ValueError("Chưa cấu hình Gemini API Key.")

        # Ưu tiên sử dụng google-genai SDK nếu có
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.7,
                ),
            )
            if resp and resp.text:
                return resp.text.strip()
        except Exception as sdk_err:
            # Fallback sang REST API chuẩn của Google Generative Language
            add_log("AI", f"google-genai SDK lỗi, chuyển sang REST API: {sdk_err}", "WARNING")
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            payload = {
                "systemInstruction": {
                    "parts": [{"text": system_instruction}]
                },
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": prompt}],
                    }
                ],
                "generationConfig": {
                    "temperature": 0.7,
                },
            }
            r = requests.post(
                url,
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
                json=payload,
                timeout=30,
            )
            r.raise_for_status()
            j = r.json()
            return j["candidates"][0]["content"]["parts"][0]["text"].strip()

    if prov in ("openai", "chatgpt"):
        api_key = settings.get("openai_api_key", "").strip()
        model = settings.get("openai_model", "gpt-4.1")
        if not api_key:
            raise ValueError("Chưa cấu hình OpenAI API Key trong phần Cài đặt.")
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.7,
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    if prov == "deepseek":
        api_key = settings.get("deepseek_api_key", "").strip()
        model = settings.get("deepseek_model", "deepseek-chat")
        if not api_key:
            raise ValueError("Chưa cấu hình DeepSeek API Key trong phần Cài đặt.")
        r = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.7,
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    return translate_google_free(prompt, "vi")


def _fallback_translate_metadata(title: str, description: str, target_lang: str) -> Dict[str, str]:
    """Fallback an toàn: chỉ dịch riêng Title và Description, không bao giờ dịch câu lệnh Prompt."""
    new_title = translate_google_free(title, target_lang).strip()
    new_desc = translate_google_free(description or title, target_lang).strip()
    if "#shorts" not in new_title.lower() and len(new_title) <= 90:
        new_title = f"{new_title} #shorts".strip()
    return {
        "title": new_title[:100],
        "description": new_desc,
        "tags": "shorts, trending, viral",
    }


def _preserve_source_links(original: str, generated: str, limit: int = 5000) -> tuple[str, int]:
    """Keep links from the source description (including affiliate URLs) in generated metadata."""
    links = list(dict.fromkeys(re.findall(r"https?://[^\s<>\"']+", str(original or ""), flags=re.IGNORECASE)))
    result = str(generated or "").strip()
    missing = [url for url in links if url.casefold() not in result.casefold()]
    suffix = ""
    preserved = 0
    for url in missing:
        candidate = suffix + ("\n" if suffix else "") + url
        if len(candidate) > limit:
            continue
        suffix = candidate
        preserved += 1
    if suffix:
        separator = "\n\n" if result else ""
        available = max(0, limit - len(separator) - len(suffix))
        result = result[:available].rstrip() + separator + suffix
    return result[:limit], preserved


def ai_optimize_youtube_metadata(
    title: str,
    description: str = "",
    action: str = "rewrite_seo",  # 'translate' | 'rewrite_seo' | 'shorts_hook'
    provider: str = "gemini",
    target_lang: str = "Tiếng Việt",
    custom_prompt: str = "",
) -> Dict[str, Any]:
    """
    Dịch hoặc viết lại trọn bộ Title, Description, Tags chuẩn SEO YouTube / Shorts.
    """
    if provider == "google_translate" or (action == "translate" and provider == "google_translate"):
        result = _fallback_translate_metadata(title, description, target_lang)
        result["title"] = result["title"][:100]
        result["description"], result["links_preserved"] = _preserve_source_links(description, result["description"])
        result["provider_used"] = "google_translate"
        result["fallback"] = False
        return result

    if action == "shorts_hook":
        default_inst = (
            f"Bạn là chuyên gia sáng tạo nội dung YouTube Shorts triệu views. Hãy viết lại thông tin video sau sang [{target_lang}]:\n"
            f"1. Tiêu đề NGẮN GỌN (DƯỚI 55 KÝ TỰ để hiển thị trọn vẹn trên app điện thoại mà không bị che khuất).\n"
            f"2. Tạo câu mở đầu (Hook) kích thích trí tò mò cực độ, gây bất ngờ, kích thích người xem nán lại xem hết video.\n"
            f"3. Bắt buộc có hashtag #shorts và 1 hashtag hot trend (ví dụ: #shorts #viral hoặc #shorts #xuhuong).\n"
            f"4. Mô tả ngắn 2-3 câu kêu gọi đăng ký kênh hoặc xem hết video.\n"
            f"5. 8-10 từ khóa tags phân cách bởi dấu phẩy."
        )
    else:
        default_inst = "Tạo tiêu đề hấp dẫn kích thích click (CTR cao, tối đa 95 ký tự, có 2 hashtag #shorts), mô tả cuốn hút chuẩn SEO và 10 từ khóa tags cách nhau bởi dấu phẩy."

    prompt = (
        f"Hãy xử lý thông tin video dưới đây sang ngôn ngữ [{target_lang}] để đăng lên YouTube:\n"
        f"- Tiêu đề gốc: {title}\n"
        f"- Mô tả gốc: {description}\n"
        f"- Yêu cầu: {custom_prompt or default_inst}\n\n"
        "Trả về ĐÚNG định dạng JSON sau (không dùng markdown block khác):\n"
        '{"title": "...", "description": "...", "tags": "tag1, tag2, tag3"}'
    )

    try:
        raw = call_llm(prompt, provider=provider)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            parsed = json.loads(m.group(0))
            result = {
                "title": str(parsed.get("title") or title)[:100],
                "description": str(parsed.get("description") or description or title),
                "tags": str(parsed.get("tags") or "shorts, viral"),
            }
            result["description"], result["links_preserved"] = _preserve_source_links(description, result["description"])
            result["provider_used"] = provider
            result["fallback"] = False
            return result
    except Exception as ex:
        add_log("AI", f"AI ({provider}) chuyển sang dịch nhanh Google Translate: {ex}", "WARNING")
        result = _fallback_translate_metadata(title, description, target_lang)
        result["description"], result["links_preserved"] = _preserve_source_links(description, result["description"])
        result["provider_used"] = "google_translate"
        result["fallback"] = True
        result["fallback_reason"] = "Nhà cung cấp được chọn không trả kết quả hợp lệ; đã dùng Google Translate."
        return result

    add_log("AI", f"AI ({provider}) trả nội dung không đúng định dạng; chuyển sang Google Translate.", "WARNING")
    result = _fallback_translate_metadata(title, description, target_lang)
    result["description"], result["links_preserved"] = _preserve_source_links(description, result["description"])
    result["provider_used"] = "google_translate"
    result["fallback"] = True
    result["fallback_reason"] = "Nhà cung cấp được chọn trả nội dung không đúng định dạng; đã dùng Google Translate."
    return result


def apply_batch_text_edit(
    text: str,
    operation: str,
    find_str: str = "",
    replace_str: str = "",
) -> str:
    """
    Biên tập văn bản hàng loạt (tương đương form CZXxuN của DHB):
    - append_first: Thêm vào đầu
    - append_end: Thêm vào cuối
    - replace: Tìm và thay thế
    - strip_hashtags: Xóa toàn bộ #hashtag cũ
    - uppercase / lowercase / titlecase
    """
    val = text or ""
    if operation == "append_first":
        return f"{replace_str} {val}".strip()
    elif operation == "append_end":
        return f"{val} {replace_str}".strip()
    elif operation == "replace":
        if find_str:
            return val.replace(find_str, replace_str)
        return val
    elif operation == "strip_hashtags":
        cleaned = re.sub(r"#\S+", "", val)
        return re.sub(r"\s+", " ", cleaned).strip()
    elif operation == "uppercase":
        return val.upper()
    elif operation == "lowercase":
        return val.lower()
    elif operation == "titlecase":
        return val.title()
    return val
