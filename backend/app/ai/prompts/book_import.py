"""拆书反推的提示词：通读（book_import.digest）与汇总（book_import.synthesize）。

数据块由 services.book_import.reverse 拼好后传入。
"""

from app.ai.prompts.common import JSON_ONLY_RULE
from app.ai.prompts.formats import BOOK_DIGEST_FORMAT, BOOK_SYNTHESIZE_FORMAT
from app.ai.prompts.registry import render
from app.ai.providers.base import ChatMessage
from app.ai.templating import PromptOverride


def build_book_digest_messages(
    *, project_info: str, known_characters: str, chapters_text: str, override: PromptOverride | None = None
) -> list[ChatMessage]:
    return render(
        "book_import.digest",
        override,
        {
            "project_info": project_info.strip(),
            "known_characters": known_characters.strip(),
            "chapters_text": chapters_text.strip(),
            "json_rule": JSON_ONLY_RULE,
            "output_format": BOOK_DIGEST_FORMAT,
        },
    )


def build_book_synthesize_messages(
    *,
    project_info: str,
    summaries: str,
    characters: str,
    organizations: str,
    world: str,
    careers: str,
    relations: str,
    override: PromptOverride | None = None,
) -> list[ChatMessage]:
    return render(
        "book_import.synthesize",
        override,
        {
            "project_info": project_info.strip(),
            "summaries": summaries.strip(),
            "characters": characters.strip(),
            "organizations": organizations.strip(),
            "world": world.strip(),
            "careers": careers.strip(),
            "relations": relations.strip(),
            "json_rule": JSON_ONLY_RULE,
            "output_format": BOOK_SYNTHESIZE_FORMAT,
        },
    )
