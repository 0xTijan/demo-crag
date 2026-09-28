def clean_json_string(text: str) -> str:
    """Strips Markdown code fences (```json ... ```) if present."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove top ``` or ```json
        if lines[0].startswith("```"):
            lines = lines[1:]
        # Remove bottom ```
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()

def history_to_string(history: list) -> str:
    if not history:
        return "None"
    return "\n".join(f"{item['role']}: {item['content']}" for item in history)