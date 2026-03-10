"""Analyzes conversation history, personal profile, and user interactions to build and
maintain a detailed, categorized inventory of Jesse's skills, with emphasis on tax,
accounting, and unemployment insurance expertise.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Dict, Any, Optional

from src.kernel.model_interface import call_model

from capabilities.personal_profile_manager import get_manager
from capabilities.timestamped_chat_history_recall import recall_messages_in_range


class DetailedSkillsInventoryBuilder:
    def __init__(self, profile_path: Optional[Path] = None):
        self.profile_path = profile_path or get_manager().profile_path

    def _load_profile(self) -> Dict[str, Any]:
        with self.profile_path.open("r") as f:
            return json.load(f)

    def _save_profile(self, profile: Dict[str, Any]) -> None:
        with self.profile_path.open("w") as f:
            json.dump(profile, f, indent=2)

    def build_inventory(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        user_id = user_id or os.getenv("JESSE_DISCORD_ID") or "jesse"
        end = time.time()
        start = end - 365 * 86400.0
        msgs = recall_messages_in_range(user_id, start, end)
        keywords = r"\b(skill|skills|tax|accounting|unemployment|insurance|expert|proficient|experience|work|job|career)\b"
        relevant = [
            m for m in msgs if re.search(keywords, m.get("content", ""), re.IGNORECASE)
        ]
        history_text = "\n".join(
            [f"{m['role'].upper()}: {m['content']}" for m in relevant[-50:]]
        )
        profile = self._load_profile()
        profile_text = json.dumps(profile, indent=2)
        system = """You are an expert skills assessor. Categorize Jesse's skills by domain (e.g., Tax, Accounting, Unemployment Insurance). For each: proficiency ("novice","intermediate","advanced","expert"), description (1 sentence), examples (list 2-5), subskills (list). Output ONLY JSON {"domains": {"Domain": {...}, ...}}"""
        prompt = f"Profile:\n{profile_text}\n\nHistory:\n{history_text}\n\nSynthesize inventory."
        resp = call_model(prompt, system=system)
        try:
            inv = json.loads(resp.text.strip())
            if "domains" not in inv:
                raise ValueError("Missing domains")
        except Exception:
            inv = {"domains": {}}
        profile["skills"] = inv
        self._save_profile(profile)
        return inv

    def query_skills(self, domain_filter: Optional[str] = None) -> Dict[str, Any]:
        profile = self._load_profile()
        skills = profile.get("skills", {}).get("domains", {})
        if domain_filter:
            skills = {
                k: v
                for k, v in skills.items()
                if domain_filter.lower() in k.lower()
            }
        return skills

    def periodic_update(self) -> None:
        self.build_inventory()