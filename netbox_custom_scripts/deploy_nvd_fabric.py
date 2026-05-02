"""
NetBox Custom Script — ``Deploy NVD Fabric``

Drop this file into ``/opt/netbox/netbox/scripts/`` on the NetBox host
and restart ``netbox-rq`` (NetBox auto-discovers scripts in
``SCRIPTS_ROOT``). The operator then runs it from the NetBox UI under
Customization → Scripts.

Architecture (deliberate):

- NetBox's own venv stays clean. The NVD automation engine lives in its
  own venv at ``/opt/nvd/.venv`` and this Custom Script shells out to it
  via ``subprocess.Popen`` with line-buffered stdout/stderr. Every line
  the engine prints is echoed via ``self.log_info`` so the operator
  watches a live log in the NetBox script-output pane.
- Deployment feedback is written back into NetBox:
    - Site custom fields (``nvd_deployment_state``,
      ``nvd_last_deploy_at``, ``nvd_last_deploy_txid``,
      ``nvd_last_deploy_phase``, ``nvd_last_deploy_error``,
      ``nvd_last_deploy_summary``, ``nvd_last_deploy_duration``).
    - A Journal Entry appended to the Site with a markdown body that
      includes the transaction id, phases, counts, nodes affected,
      duration, and any intent-level error messages.
- Only the final ``[NVD-DEPLOY-SUMMARY] {...}`` line produced by
  ``automation.deploy`` is parsed — we never scrape the log.

Environment expected on the NetBox worker (usually set in
``/etc/systemd/system/netbox-rq.service.d/nvd.conf``):

    NVD_VENV=/opt/nvd/.venv
    NVD_REPO=/opt/nvd/src
    NETBOX_URL=https://srv9002
    NETBOX_TOKEN=nbt_...
    EDA_URL=https://srv0201:9443
    EDA_USER=admin
    EDA_PASSWORD=admin
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import datetime, timezone

from dcim.models import Site
from extras.scripts import (
    BooleanVar,
    ChoiceVar,
    MultiChoiceVar,
    ObjectVar,
    Script,
    StringVar,
)

# Marker printed by automation/deploy.py as the final stdout line.
DEPLOY_SUMMARY_MARKER = "[NVD-DEPLOY-SUMMARY]"

# NVD engine location — adjust via env in the systemd drop-in if needed.
DEFAULT_NVD_VENV = "/opt/nvd/.venv"
DEFAULT_NVD_REPO = "/opt/nvd/src"


class DeployFabric(Script):
    """Run the NVD automation engine for a NetBox-sourced fabric."""

    class Meta:
        name = "Deploy NVD Fabric"
        description = (
            "Build a FabricIntent from a NetBox Site and deploy it via the "
            "NVD automation engine (EDA or Ansible). Runs as a subprocess "
            "against /opt/nvd/.venv."
        )
        commit_default = True
        fieldsets = (
            ("Target", ("site", "mode")),
            ("Options", ("dry_run", "phases", "prune")),
        )

    site = ObjectVar(
        description="NetBox Site to deploy (must have nvd_design custom field set).",
        model=Site,
    )
    mode = ChoiceVar(
        description="Deployment mode.",
        choices=(("eda", "EDA"), ("ansible", "Ansible (project generation)")),
        default="eda",
    )
    dry_run = BooleanVar(
        description="EDA dry-run (validate only). Strongly recommended for first run.",
        default=True,
    )
    phases = MultiChoiceVar(
        description="Restrict to specific deployment phases (leave empty for all).",
        choices=(
            ("topology", "Topology"),
            ("fabric", "Fabric"),
            ("services", "Services"),
        ),
        required=False,
    )
    prune = BooleanVar(
        description="Delete EDA resources not in the desired state.",
        default=False,
    )

    # -----------------------------------------------------------------------
    # Entry point
    # -----------------------------------------------------------------------

    def run(self, data, commit):
        site: Site = data["site"]
        mode: str = data["mode"]
        dry_run: bool = bool(data["dry_run"])
        phases: list[str] = list(data.get("phases") or [])
        prune: bool = bool(data["prune"])

        self.log_info(f"Starting NVD deploy for site **{site.slug}** in `{mode}` mode")

        # Mark state = deploying
        _update_site_cf(
            site,
            {
                "nvd_deployment_state": "deploying",
                "nvd_last_deploy_at": _now_iso(),
                "nvd_last_deploy_error": "",
                "nvd_last_deploy_phase": ",".join(phases) if phases else "all",
            },
        )

        nvd_venv = os.environ.get("NVD_VENV", DEFAULT_NVD_VENV)
        nvd_repo = os.environ.get("NVD_REPO", DEFAULT_NVD_REPO)
        python_bin = os.path.join(nvd_venv, "bin", "python")

        if not os.path.exists(python_bin):
            msg = f"NVD venv Python not found at {python_bin}"
            self.log_failure(msg)
            _update_site_cf(
                site,
                {
                    "nvd_deployment_state": "failed",
                    "nvd_last_deploy_error": msg,
                },
            )
            _append_journal(
                site,
                kind="danger",
                body=f"**Deploy aborted**: {msg}\n",
            )
            return "failed"

        cmd = [
            python_bin,
            "-m",
            "automation.deploy",
            "--source",
            "netbox",
            "--site",
            site.slug,
            "--mode",
            mode,
        ]
        if dry_run:
            cmd.append("--dry-run")
        if prune:
            cmd.append("--prune")
            cmd.append("--yes")
        for p in phases:
            cmd.extend(["--phase", p])

        # Credentials and URLs come from the rq worker's environment
        # (see systemd drop-in). We do NOT log them.
        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")

        self.log_info(f"exec: `{shlex.join(cmd)}` (cwd={nvd_repo})")

        summary, stdout_tail, rc = _run_streaming(
            cmd, cwd=nvd_repo, env=env, logger=self,
        )

        # Persist result
        state = "deployed" if summary.get("success") else "failed"
        if rc != 0 and not summary:
            state = "failed"

        summary_text = _build_summary_text(summary)
        cf_updates = {
            "nvd_deployment_state": state,
            "nvd_last_deploy_at": _now_iso(),
            "nvd_last_deploy_txid": summary.get("transaction_id") or "",
            "nvd_last_deploy_phase": ",".join(phases) if phases else "all",
            "nvd_last_deploy_summary": summary_text[:255],
            "nvd_last_deploy_duration": int(summary.get("duration_s") or 0),
            "nvd_last_deploy_error": _first_error(summary, stdout_tail)[:500],
        }
        _update_site_cf(site, cf_updates)

        # Journal entry
        kind = _journal_kind(state, mode, dry_run)
        body = _render_journal(summary, mode, dry_run, phases, rc, stdout_tail)
        _append_journal(site, kind=kind, body=body)

        if state == "deployed":
            self.log_success(
                f"{'Dry-run' if dry_run else 'Deployment'} successful "
                f"(txid={summary.get('transaction_id') or 'n/a'})"
            )
            return summary_text
        self.log_failure(
            f"Deploy FAILED (rc={rc}) — see Site journal for details"
        )
        return summary_text


# ---------------------------------------------------------------------------
# Subprocess streaming
# ---------------------------------------------------------------------------


def _run_streaming(cmd, *, cwd, env, logger):
    """Run ``cmd``, stream each stdout/stderr line into ``logger``, and
    return (summary_dict, stdout_tail_list, returncode)."""
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
    )
    summary: dict = {}
    tail: list[str] = []
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            # The marker is the last line emitted by deploy.py and contains
            # the JSON summary we parse back into NetBox.
            if DEPLOY_SUMMARY_MARKER in line:
                after = line.split(DEPLOY_SUMMARY_MARKER, 1)[1].strip()
                try:
                    summary = json.loads(after)
                except Exception as e:
                    logger.log_warning(f"Could not parse summary JSON: {e}")
            else:
                logger.log_info(line)
            tail.append(line)
            if len(tail) > 200:
                tail.pop(0)
    finally:
        rc = proc.wait()
    return summary, tail, rc


# ---------------------------------------------------------------------------
# Custom-field and journal writers
# ---------------------------------------------------------------------------


def _update_site_cf(site: Site, fields: dict) -> None:
    """Merge ``fields`` into ``site.custom_field_data`` and persist."""
    current = dict(getattr(site, "custom_field_data", {}) or {})
    current.update(fields)
    site.custom_field_data = current
    site.save()


def _append_journal(site: Site, *, kind: str, body: str) -> None:
    """Append a Journal Entry to the Site via the ORM so it shows up under
    the Site's Journal tab."""
    try:
        from extras.models import JournalEntry
        from django.contrib.contenttypes.models import ContentType
        ct = ContentType.objects.get_for_model(site.__class__)
        JournalEntry.objects.create(
            assigned_object_type=ct,
            assigned_object_id=site.id,
            kind=kind,
            comments=body,
        )
    except Exception:
        # Journal is nice-to-have; never break the script over it.
        pass


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build_summary_text(summary: dict) -> str:
    if not summary:
        return "deploy failed before summary was emitted"
    parts = []
    parts.append(f"mode={summary.get('mode', '?')}")
    if summary.get("dry_run"):
        parts.append("dry_run=yes")
    if summary.get("creates") or summary.get("updates") or summary.get("deletes"):
        parts.append(
            f"creates={summary.get('creates', 0)} "
            f"updates={summary.get('updates', 0)} "
            f"deletes={summary.get('deletes', 0)}"
        )
    if summary.get("nodes_affected"):
        parts.append(f"nodes={len(summary['nodes_affected'])}")
    parts.append(f"duration={summary.get('duration_s', 0)}s")
    return " ".join(parts)


def _first_error(summary: dict, stdout_tail: list[str]) -> str:
    if summary:
        errs = summary.get("errors") or []
        if errs:
            return "; ".join(errs)
    for line in reversed(stdout_tail):
        if "ERROR" in line or "FAILED" in line:
            return line
    return ""


def _journal_kind(state: str, mode: str, dry_run: bool) -> str:
    if state == "failed":
        return "danger"
    if dry_run:
        return "warning"
    if mode == "ansible":
        return "info"
    return "success"


def _render_journal(
    summary: dict,
    mode: str,
    dry_run: bool,
    phases: list[str],
    rc: int,
    stdout_tail: list[str],
) -> str:
    lines: list[str] = []
    lines.append(f"**NVD deploy** — mode=`{mode}` dry_run=`{dry_run}` rc=`{rc}`")
    if not summary:
        lines.append("")
        lines.append("_deploy exited before a summary line was produced._")
        lines.append("")
        lines.append("Last stdout lines:")
        lines.append("```")
        lines.extend(stdout_tail[-30:])
        lines.append("```")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"- **Site**: `{summary.get('site', '?')}`")
    if summary.get("transaction_id"):
        lines.append(f"- **Transaction**: `{summary['transaction_id']}`")
    lines.append(f"- **Design**: `{summary.get('design', '?')}`")
    lines.append(f"- **Phases**: `{', '.join(phases) if phases else 'all'}`")
    lines.append(
        f"- **Resource ops**: creates=`{summary.get('creates', 0)}`, "
        f"updates=`{summary.get('updates', 0)}`, "
        f"deletes=`{summary.get('deletes', 0)}`"
    )
    if summary.get("nodes_affected"):
        lines.append(
            f"- **Nodes affected** ({len(summary['nodes_affected'])}): "
            + ", ".join(f"`{n}`" for n in summary["nodes_affected"])
        )
    lines.append(f"- **Duration**: {summary.get('duration_s', 0)} s")

    if summary.get("errors"):
        lines.append("")
        lines.append("**Errors**:")
        for err in summary["errors"]:
            lines.append(f"  - `{err}`")
    return "\n".join(lines)
