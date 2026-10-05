# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Why the last start held the stored records back, in plain words.

A start that cannot finish an update step boots anyway with changes to records
paused (``app.state.data_current`` is False). ``HeldBack`` is the one record of what
failed: the notice in the notification bell, the refusal a change gets, and the
Doctor report are all written from it, so they always name the same cause.
"""

from __future__ import annotations

from dataclasses import dataclass

TITLE = "An update step failed while Celerp started"

# What waits while the records are held back: everything that reads them to change
# data or tell other systems (celerp.main starts none of it until they are current).
_WAITING = ("sending stock levels to your online stores, the daily store sync, "
            "low-stock alerts, and the start-up work of your modules, such as settling "
            "manufacturing runs")
_DOCTOR = ("open Doctor in the sidebar (it only reports, it changes nothing), where the "
           "failed step and its error are shown, then use Report a bug in your account "
           "menu and include what Doctor shows")


@dataclass(frozen=True)
class Failure:
    """One update step that failed: its plain name and the error it raised."""

    step: str
    error: str


@dataclass(frozen=True)
class HeldBack:
    """What held the last start back.

    ``disabled`` names the installed modules that are turned off while the records hold
    their records; when that is the whole cause, enabling them is the fix. ``failures``
    are the steps that failed for any other reason.
    """

    failures: tuple[Failure, ...] = ()
    disabled: tuple[str, ...] = ()

    @property
    def _steps(self) -> str:
        return "; ".join(f.step for f in self.failures)

    @property
    def _modules(self) -> str:
        return ", ".join(self.disabled)

    @property
    def action_url(self) -> str:
        return "/modules" if self.disabled and not self.failures else "/doctor"

    def _what_to_do(self) -> str:
        first = f"Enable {self._modules} in Modules, then restart Celerp." if self.disabled else "Restart Celerp."
        return f"{first} If this notice comes back, {_DOCTOR}."

    def _what_failed(self) -> str:
        parts = []
        if self.disabled:
            parts.append(f"your records include records of {self._modules}, which "
                         f"{'is' if len(self.disabled) == 1 else 'are'} turned off in Modules, "
                         "so they could not be brought up to date for this release")
        if self.failures:
            parts.append(f"this step did not finish: {self._steps}")
        return "; and ".join(parts)

    def notice(self) -> str:
        """The body of the notice in the notification bell."""
        return (f"An update step failed while Celerp started: {self._what_failed()}. "
                "You can still view all your records. Changes to records are paused, and so is "
                f"the work that depends on them: {_WAITING}. What to do: {self._what_to_do()}")

    def refusal(self) -> str:
        """Why a change to a record is refused while the records are held back."""
        if self.disabled and not self.failures:
            return (f"Changes are paused because {self._modules} "
                    f"{'is' if len(self.disabled) == 1 else 'are'} turned off and your records "
                    f"include {'its' if len(self.disabled) == 1 else 'their'} records. You can still "
                    f"view records. Enable {self._modules} in Modules, then restart Celerp.")
        return ("Changes are paused because an update step failed while Celerp started "
                f"({self._steps or self._what_failed()}). You can still view records. "
                f"{self._what_to_do()}")

    def report(self) -> dict:
        """What Doctor shows: every failed step with its error."""
        failures = [{"step": f.step, "error": f.error} for f in self.failures]
        failures += [{"step": f"Reading the records of {m}",
                      "error": f"{m} is turned off in Modules"} for m in self.disabled]
        return {"title": TITLE, "failures": failures, "what_to_do": self._what_to_do()}


# A start that held back without saying why (only when the flag was set directly).
UNKNOWN = HeldBack((Failure("Updating the stored records to this release",
                            "the cause was not recorded"),))


def held_back(app) -> HeldBack | None:
    """Why the app's last start held the records back, or None when they are current."""
    state = getattr(app, "state", None)
    if state is None or getattr(state, "data_current", True):
        return None
    return getattr(state, "held_back", None) or UNKNOWN
