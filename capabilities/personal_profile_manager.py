"""Maintains a structured JSON profile of Jesse's self-reported skills, job details, location, preferences, and goals by parsing conversation history and prompting for clarifications via Discord DMs when gaps are detected."""

import json
import os
from pathlib import Path
from typing import Dict, Any, List, Optional

from src.kernel.capability_registry import Capability, CapabilityRegistry
from src.kernel.model_interface import call_model

from capabilities.conversational_memory import get_context
from capabilities.discord_notifier import notify


JESSE_USER_ID: str = os.getenv("JESSE_DISCORD_ID", "")
PROFILE_PATH = Path("data") / "personal_profile.json"


class PersonalProfileManager:
    """Manages Jesse's personal profile JSON."""

    def __init__(self, profile_path: Path = PROFILE_PATH):
        self.profile_path = profile_path
        self.profile = self._load()

    def _load(self) -> Dict[str, Any]:
        if self.profile_path.exists():
            with open(self.profile_path, encoding="utf-8") as f:
                return json.load(f)
        return {
            "skills": [],
            "job_details": {},
            "location": "",
            "preferences": {},
            "goals": [],
        }

    def _save(self) -> None:
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.profile_path, "w", encoding="utf-8") as f:
            json.dump(self.profile, f, indent=2)

    def update_from_conversation(self) -> None:
        context = get_context(JESSE_USER_ID).strip()
        if not context:
            return
        prompt = f"""Update Jesse's profile JSON from this context. Merge with current profile without removing fields.

Current: {json.dumps(self.profile, indent=2)}

Context: {context}

Respond with ONLY valid JSON profile:"""
        try:
            resp = call_model(prompt)
            new_profile = json.loads(resp.text.strip())
            self.profile.update(new_profile)
            self._save()
        except Exception:
            pass

    def detect_gaps(self) -> List[str]:
        required = ["location", "skills", "job_details", "preferences", "goals"]
        gaps = []
        for field in required:
            val = self.profile.get(field)
            if (
                val is None
                or (isinstance(val, (list, dict)) and not val)
                or (isinstance(val, str) and not val.strip())
            ):
                gaps.append(field.replace("_", " ").title())
        return gaps

    def prompt_for_gaps(self) -> None:
        gaps = self.detect_gaps()
        if not gaps:
            return
        text = (
            f"Hi Jesse! Profile gaps detected: {', '.join(gaps)}. "
            "Please share details to help me assist you better!"
        )
        notify(text)


def get_manager() -> PersonalProfileManager:
    return PersonalProfileManager()


def periodic_update() -> None:
    """Run profile update and gap prompting. Schedule periodically (e.g., hourly)."""
    manager = get_manager()
    manager.update_from_conversation()
    manager.prompt_for_gaps()


def register_capability(
    registry: Optional[CapabilityRegistry] = None,
) -> Optional[Capability]:
    if registry is None:
        return None
    cap = Capability(
        name="personal_profile_manager",
        module="capabilities.personal_profile_manager",
        description=__doc__.strip(),
        dependencies=["conversational_memory", "discord_notifier"],
    )
    registry.add(cap)
    return cap