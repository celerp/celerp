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

from celerp.accounting_roles import refusal

TITLE = "An update step failed while Celerp started"

# The sidebar and account-menu items the notice sends the reader to, by the keys the
# sidebar and the account menu show them under.
_MODULES = refusal("nav.modules", "Modules")
_DOCTOR = refusal("nav.doctor", "Doctor")
_REPORT_BUG = refusal("nav.report_bug", "Report a bug")


def _step(key: str, message: str, **params) -> dict:
    return refusal(f"held_back.step.{key}", message, **params)


def _error(key: str, message: str, **params) -> dict:
    return refusal(f"held_back.error.{key}", message, **params)


UPDATE_STEP = _step("update", "Updating the stored records to this release")
UNOWNED_STEP = _step("unowned", "Reading records written by a module that is not installed")
SAVING_STEP = _step("saving", "Saving the start-up work of the enabled modules")
NOT_CURRENT = _error("not_current", "The stored records were not brought up to date")


def module_start_step(module: str) -> dict:
    return _step("module_start", f"Starting the {module} module", module=module)


def unowned_error(event_types) -> dict:
    events = ", ".join(event_types)
    return _error("unowned", f"No installed module handles these records: {events}", events=events)


@dataclass(frozen=True)
class Failure:
    """One update step that failed: the step, as a keyed message, and the error it
    raised (as the exception put it, or a keyed message)."""

    step: dict
    error: str | dict


@dataclass(frozen=True)
class HeldBack:
    """What held the last start back, as keyed messages read in the reader's language.

    ``disabled`` names the installed modules that are turned off while the records hold
    their records; when that is the whole cause, enabling them is the fix. ``failures``
    are the steps that failed for any other reason.
    """

    failures: tuple[Failure, ...] = ()
    disabled: tuple[str, ...] = ()

    @property
    def _modules(self) -> str:
        return ", ".join(self.disabled)

    @property
    def _count(self) -> str:
        return "one" if len(self.disabled) == 1 else "many"

    @property
    def action_url(self) -> str:
        return "/modules" if self.disabled and not self.failures else "/doctor"

    def _enable(self) -> dict:
        return refusal("held_back.enable", f"Enable {self._modules} in Modules, then restart Celerp.",
                       modules=self._modules, modules_page=_MODULES)

    def _what_to_do(self) -> dict:
        first = self._enable() if self.disabled else refusal("held_back.restart", "Restart Celerp.")
        return refusal(
            "held_back.what_to_do",
            f"{first['message']} If this notice comes back, open Doctor in the sidebar (it only "
            "reports, it changes nothing), where the failed step and its error are shown, then use "
            "Report a bug in your account menu and include what Doctor shows. If you are not an "
            "admin, ask an admin to do this.",
            first=first, doctor=_DOCTOR, report_bug=_REPORT_BUG)

    def _what_failed(self) -> list[dict]:
        parts = []
        if self.disabled:
            verb = "is" if len(self.disabled) == 1 else "are"
            parts.append(refusal(
                f"held_back.modules_off.{self._count}",
                f"Your records include records of {self._modules}, which {verb} turned off in "
                "Modules, so they could not be brought up to date for this release.",
                modules=self._modules, modules_page=_MODULES))
        if self.failures:
            steps = [f.step for f in self.failures]
            parts.append(refusal("held_back.step_failed",
                                 f"This step did not finish: {'; '.join(s['message'] for s in steps)}.",
                                 steps=steps))
        return parts

    def notice(self) -> dict:
        """The body of the notice in the notification bell, read under its title ``TITLE``."""
        what_failed, what_to_do = self._what_failed(), self._what_to_do()
        return refusal(
            "held_back.notice",
            f"{' '.join(p['message'] for p in what_failed)} You can still view all your records. "
            "Changes to records are paused, and so is the work that depends on them: sending stock "
            "levels to your online stores, the daily store sync, low-stock alerts, and the start-up "
            "work of your modules, such as settling manufacturing runs. What to do: "
            f"{what_to_do['message']}",
            what_failed=what_failed, what_to_do=what_to_do)

    def notice_keys(self) -> dict:
        """The message keys the notice is shown from in the reader's language."""
        notice = self.notice()
        return {"title": "held_back.title", "body": notice["message_key"], "params": notice["params"]}

    def refusal(self) -> dict:
        """Why a change to a record is refused while the records are held back."""
        if self.disabled and not self.failures:
            pronoun = "its" if len(self.disabled) == 1 else "their"
            verb = "is" if len(self.disabled) == 1 else "are"
            enable = self._enable()
            return refusal(
                f"held_back.refused.modules_off.{self._count}",
                f"Changes are paused because {self._modules} {verb} turned off and your records "
                f"include {pronoun} records. You can still view records. {enable['message']} "
                "If you are not an admin, ask an admin to do this.",
                modules=self._modules, enable=enable)
        what_failed, what_to_do = self._what_failed(), self._what_to_do()
        return refusal(
            "held_back.refused",
            "Changes are paused because an update step failed while Celerp started. "
            f"{' '.join(p['message'] for p in what_failed)} You can still view records. "
            f"{what_to_do['message']}",
            what_failed=what_failed, what_to_do=what_to_do)

    def report(self) -> dict:
        """What Doctor shows: every failed step with its error."""
        failures = [{"step": f.step, "error": f.error} for f in self.failures]
        failures += [{"step": _step("read_module", f"Reading the records of {m}", module=m),
                      "error": _error("module_off", f"{m} is turned off in Modules",
                                      module=m, modules_page=_MODULES)} for m in self.disabled]
        return {"title": refusal("held_back.title", TITLE), "failures": failures,
                "what_to_do": self._what_to_do()}


# A start that held back without saying why (only when the flag was set directly).
UNKNOWN = HeldBack((Failure(UPDATE_STEP, _error("unrecorded", "the cause was not recorded")),))


def held_back(app) -> HeldBack | None:
    """Why the app's last start held the records back, or None when they are current."""
    state = getattr(app, "state", None)
    if state is None or getattr(state, "data_current", True):
        return None
    return getattr(state, "held_back", None) or UNKNOWN
