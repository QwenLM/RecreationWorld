#!/usr/bin/env python3
"""Render the recreation instruction for every supported platform.

There is one template and one public ``render`` function. Platform differences live in the
substitution table as explicit blocks; no platform owns a second task prompt. This keeps shared
behavior genuinely shared without pretending that desktop binaries, Android APKs, and rendered
web pages have identical isolation or build constraints.

An absent substitution is an error. An intentionally empty platform block must be present as
``""`` so that adding a placeholder cannot silently remove a security or delivery rule.

Runtimes may provide only the values needed to resolve placeholders such as app name, device ID,
or local URL. They must not append tool catalogues or a second system-level task instruction.
"""

from __future__ import annotations

import re

from core.recreation_paths import WorkspacePathError, workspace_paths

PLATFORMS = ("linux", "macos", "windows", "android", "web")

PLACEHOLDERS = (
    "platform",
    "platform_context",
    "environment",
    "observation_tool",
    "accessibility_tree",
    "fixtures_description",
    "task_scope",
    "observation_guidance",
    "fidelity_targets",
    "identity_requirement",
    "resource_policy",
    "deliverables",
    "build_requirements",
    "source_isolation_rules",
    "accessibility_contract",
    "security_rules",
    "platform_guidance",
    "process_rule",
    "conflict_avoidance",
    "final_checklist",
)

TEMPLATE = "\n".join(
    (
        "You are recreating a {platform} application from observation.",
        ("The reference is a local offline copy. Nothing is published or distributed."),
        "{platform_context}",
        (
            "The reference is available in {environment}. Use the {observation_tool} tools to "
            "inspect screenshots, {accessibility_tree}, and interactive behavior. "
            "{fixtures_description}"
        ),
        "{task_scope}",
        (
            "Study the reference thoroughly before and as you write code. Inspect its visual "
            "layout, colors, structure, and behavior, and keep returning to it rather than "
            "relying on a single early look."
        ),
        "{observation_guidance}",
        (
            "Write a new, original implementation with the highest possible fidelity, matching "
            "{fidelity_targets}."
        ),
        "{identity_requirement}",
        "{resource_policy}",
        "",
        "Deliver:",
        "{deliverables}",
        "",
        "Build requirements:",
        "{build_requirements}",
        "",
        "{source_isolation_rules}",
        "{accessibility_contract}",
        "{security_rules}{platform_guidance}",
        "Important principles:",
        (
            "- Develop iteratively: start with a minimal working skeleton, build it, launch it, "
            "and verify against the reference, then keep adding and refining in small increments."
        ),
        (
            "- Be thorough: reproduce the full persistent product surface, not just the initial "
            "screen."
        ),
        '- Treat "it launches" as the start, not the finish. Keep iterating to improve fidelity.',
        "- Do not give up after a failed build or launch; diagnose the error, fix it, and retry.",
        (
            "- Verify quantitatively after each rebuild. Compare behavior and visual output "
            "against the reference rather than guessing."
        ),
        "{process_rule}",
        "{conflict_avoidance}",
        "{final_checklist}",
        (
            "This is an automated pipeline with no user interaction. You MUST include at least "
            "one tool call in every response; a text-only response terminates the session. Do not "
            "ask questions or wait for confirmation. Do NOT use Agent, Workflow, EnterPlanMode, "
            "ExitPlanMode, or AskUserQuestion."
        ),
    )
)


VALUES: dict[str, dict[str, str]] = {
    "linux": {
        "platform": "Linux desktop",
        "platform_context": "",
        "environment": "the desktop",
        "observation_tool": "computer-use",
        "accessibility_tree": "accessibility trees",
        "fixtures_description": "Test data files are available at /workspace/fixtures/.",
        "task_scope": "",
        "observation_guidance": "",
        "fidelity_targets": (
            "AT-SPI widget functionality (roles, names, actions, and element count), visual "
            "appearance (colors, layout, and geometry), and interactive behavior"
        ),
        "identity_requirement": (
            "Expose the same AT-SPI widget names and roles as the reference app."
        ),
        "resource_policy": (
            "Use any language or framework already available. Do not install packages or "
            "access the internet. Do not download, clone, or fetch external resources."
        ),
        "deliverables": (
            "1. Source code in {source_dir}/\n"
            "2. A self-contained {build_script}\n"
            "3. A {launch_script} with required runtime environment "
            "(LD_LIBRARY_PATH, GSETTINGS_SCHEMA_DIR, etc.)\n"
            "4. The compiled executable in {artifact_path}/"
        ),
        "build_requirements": (
            "- build.sh must compile only files under src/.\n"
            "- Use only files you write and libraries/tools already present in the image.\n"
            "- Do not run apt/apt-get install/update/download, clone/fetch/pull, curl, wget, "
            "pip install, npm install/exec, npx, or yarn install/add."
        ),
        "source_isolation_rules": (
            "Do not inspect or derive implementation details from the reference binary or "
            "process metadata. Do not inspect the reference through /proc or "
            "process-introspection tools."
        ),
        "accessibility_contract": (
            "\nAccessibility contract:\n"
            "- Never suppress or narrow accessibility, even if an early reference probe looks "
            "shallow. Do not set NO_AT_BRIDGE=1 or QT_ACCESSIBILITY=0, preload an AT-SPI "
            "blocking shim, disable QAccessible, hardcode AT_SPI_BUS_ADDRESS, or create a "
            "private D-Bus session. A shallow reference tree is a measurement failure to "
            "re-probe, not behavior to reproduce.\n"
        ),
        "security_rules": "",
        "platform_guidance": "",
        "process_rule": (
            "- When terminating processes, use PIDs rather than pattern-matching commands that "
            "could match your own shell."
        ),
        "conflict_avoidance": (
            "- Use unique identifiers for your application so it cannot conflict with the "
            "running reference app."
        ),
        "final_checklist": "",
    },
    "macos": {
        "platform": "macOS",
        "platform_context": "",
        "environment": "the desktop",
        "observation_tool": "computer-use",
        "accessibility_tree": "the accessibility (AX) tree",
        "fixtures_description": (
            "Approved reference assets (icons, images, fonts, and sounds) are available at "
            "~/Resources/reference/."
        ),
        "task_scope": "",
        "observation_guidance": "",
        "fidelity_targets": (
            "AX widget functionality (roles, names, actions, and element count), visual "
            "appearance (colors, layout, and geometry), and interactive behavior"
        ),
        "identity_requirement": "Expose the same AX widget names and roles as the reference app.",
        "resource_policy": (
            "Use any language or framework already available. There is no internet access; do "
            "not download, clone, or fetch external resources."
        ),
        "deliverables": (
            "1. Source code at {source_dir}/\n"
            "2. A self-contained {build_script} that builds a .app\n"
            '3. A {launch_script} that passes through "$@" and sets required '
            "runtime environment (DYLD_LIBRARY_PATH, QT_PLUGIN_PATH, etc.)\n"
            "4. The built .app copied by build.sh to {artifact_path}"
        ),
        "build_requirements": (
            "- build.sh must build only the files under {workspace_dir}/.\n"
            "- Include required local dependency setup and source patches.\n"
            "- Do not use sudo, brew install, clone/fetch/pull, curl, wget, pip install, or "
            "npm install, and never reference the reference app's path or binary."
        ),
        "source_isolation_rules": (
            "Do not inspect or derive implementation details from the reference binary or "
            "application bundle."
        ),
        "accessibility_contract": (
            "\nAccessibility contract:\n"
            "- Never suppress or narrow macOS Accessibility, even if an early reference probe "
            "looks shallow. Do not hide meaningful controls from AX, omit their labels or "
            "actions, or replace them with inaccessible custom-drawn hit regions. A shallow "
            "reference tree is a measurement failure to re-probe, not behavior to reproduce.\n"
        ),
        "security_rules": "",
        "platform_guidance": "",
        "process_rule": (
            "- When terminating processes, use PIDs rather than pattern-matching commands that "
            "could match your own shell."
        ),
        "conflict_avoidance": (
            "- Use a unique bundle identifier so the recreation cannot conflict with the "
            "running reference app."
        ),
        "final_checklist": "",
    },
    "windows": {
        "platform": "Windows desktop",
        "platform_context": "",
        "environment": "the desktop",
        "observation_tool": "computer-use",
        "accessibility_tree": "the UIA accessibility tree",
        "fixtures_description": (
            "If fixtures\\ exists in the working directory, it contains approved sample files "
            "the reference app uses or can open."
        ),
        "task_scope": "",
        "observation_guidance": "",
        "fidelity_targets": (
            "UIA widget functionality (roles, names, actions, and element count), visual "
            "appearance (colors, layout, and geometry), and interactive behavior"
        ),
        "identity_requirement": "Expose the same UIA widget names and roles as the reference app.",
        "resource_policy": (
            "Use any language or framework already available, but do not install packages or "
            "access the internet. Do not download, clone, or fetch external resources."
        ),
        "deliverables": (
            "1. Source code directly in {source_dir}\n"
            "2. {build_script}\n"
            "3. {launch_script}; it must use Start-Process -PassThru and "
            "write only the numeric PID as its first stdout line with Write-Output\n"
            "4. The executable directly in {artifact_path}\n"
            '5. build_result.json with {"success": true, "executable": "<absolute exe path>"}\n'
            "All output goes DIRECTLY in {workspace_dir}; do not create another nested "
            "recreation folder."
        ),
        "build_requirements": (
            "- build.ps1 must compile only files under src\\.\n"
            "- Use only files you write and tools already installed in the image.\n"
            "- Do not run clone/fetch/pull, curl, wget, pip install, or npm install."
        ),
        "source_isolation_rules": (
            "Do not inspect or derive implementation details from the reference binary or "
            "process memory."
        ),
        "accessibility_contract": (
            "\nAccessibility contract:\n"
            "- Never suppress or narrow UI Automation, even if an early reference probe looks "
            "shallow. Every meaningful interactive control must expose an appropriate UIA "
            "control type, name, state, and action; custom-drawn controls must provide equivalent "
            "UIA peers. A shallow reference tree is a measurement failure to re-probe, not "
            "behavior to reproduce.\n"
        ),
        "security_rules": (
            "\nHard rules:\n"
            "- Do NOT access any directory outside the working directory. The install, build, "
            "source repo, and test suite directories are ACL-protected and off-limits.\n"
            "- Do NOT launch the reference through Bash, PowerShell, Python, WMI, CIM, or "
            "scheduled tasks. Use ONLY the computer-use tools.\n"
            "- Do NOT attempt to escalate permissions with takeown, icacls, runas, schtasks, "
            "net user, cacls, or any ACL bypass.\n"
        ),
        "platform_guidance": "",
        "process_rule": (
            "- When terminating processes, use PIDs rather than process-name matching. Use "
            "the computer-use tools to restart the reference if needed."
        ),
        "conflict_avoidance": (
            "- Use a unique application name so the recreation cannot conflict with the running "
            "reference app."
        ),
        "final_checklist": "",
    },
    "android": {
        "platform": "mobile Android",
        "platform_context": "",
        "environment": "emulator {device_id}",
        "observation_tool": "computer-use",
        "accessibility_tree": "the UI hierarchy",
        "fixtures_description": "The reference APK is not available as a file.",
        "task_scope": "",
        "observation_guidance": (
            "Use only device {device_id} for every computer-use operation."
        ),
        "fidelity_targets": (
            "view IDs and types, hierarchy and element count, visible text including typos, "
            "colors, spacing, typography, icons, state, and interactive behavior"
        ),
        "identity_requirement": (
            "Use Kotlin with XML Views and ViewBinding, not Compose. Set applicationId to the "
            'observed package name plus ".clone" so both apps remain installed for comparison.'
        ),
        "resource_policy": (
            "Use only the Android SDK, Gradle wrapper, plugins, and dependencies already available "
            "locally. The fixed toolchain is Gradle 7.6.4, Android Gradle Plugin 7.4.2, Kotlin "
            "1.8.22, compileSdk/targetSdk 33, and build-tools 33.0.0. Do not change these "
            "versions. The complete local dependency catalogue is in "
            "$RB_ANDROID_OFFLINE_ROOT/DEPENDENCIES.txt. "
            "There is no internet access; do not clone, fetch, or download resources."
        ),
        "deliverables": (
            "1. All source and build files under {source_dir}/\n"
            "2. A final, non-empty APK at {artifact_path}"
        ),
        "build_requirements": (
            "- A local-only Gradle wrapper is already present in the project directory; keep "
            "it.\n"
            "- Build early with ./gradlew --offline :app:assembleDebug and keep the project "
            "compiling.\n"
            "- Install only the clone, using replace=true and grant_permissions=true.\n"
            "- If a feature prevents compilation, reduce its scope rather than ending without "
            "an APK."
        ),
        "source_isolation_rules": (
            "Do not access, copy, extract, or inspect the reference APK or its package metadata."
        ),
        "accessibility_contract": "",
        "security_rules": (
            "\nAndroid-specific rules:\n"
            "- Never uninstall the reference app.\n"
            "- If the reference asks for login, registration, phone binding, payment, or a "
            "subscription, dismiss or skip the gate and do not implement it. Treat ads, paywalls, "
            "onboarding/tutorials, rating/update prompts, and one-time privacy dialogs the same "
            "way. Reproduce persistent functional screens.\n"
        ),
        "platform_guidance": (
            "\nANDROID_HOME and ANDROID_SDK_ROOT are preconfigured. Compare screenshots and UI "
            "hierarchies screen-by-screen after each rebuild, and verify CRUD flows, settings, "
            "empty/error states, and persistence.\n"
        ),
        "process_rule": "",
        "conflict_avoidance": "",
        "final_checklist": (
            "- Before finishing, verify that {artifact_path} exists and is non-empty."
        ),
    },
    "web": {
        "platform": "web front-end",
        "platform_context": "",
        "environment": "the loopback URL {site_url}",
        "observation_tool": "computer-use",
        "accessibility_tree": "accessibility snapshots",
        "fixtures_description": "",
        "task_scope": (
            "\n## Full-site scope\n"
            "Starting from {site_url}, explore the entire site and rebuild as many pages as you "
            "can.\n\n"
            "- Discover pages by following internal links from the homepage until no new pages "
            "are found; stay on the loopback origin and ignore external links.\n"
            "- Rebuild every page that renders correctly, skipping only genuine HTTP errors, "
            "empty/error shells, and redirect loops.\n"
            "- Reuse shared components across pages while preserving each page's real content "
            "and route.\n"
        ),
        "observation_guidance": (
            "Capture desktop (1920x1080) and mobile (375x812) views."
        ),
        "fidelity_targets": (
            "visible content, semantic structure, layout, spacing, colors, typography, media, "
            "responsive behavior, navigation, and interactive states"
        ),
        "identity_requirement": (
            "Author original React/TypeScript components and class names. Reproduce visible text "
            "content, but do not copy the reference implementation."
        ),
        "resource_policy": (
            "You may copy binary media from {site_url} and its loopback origin only. Do not access "
            "external sites or CDNs. If an exact font is unavailable locally, use a bundled or "
            "system fallback."
        ),
        "deliverables": (
            "1. Source code in {source_dir}\n"
            "2. A single self-contained {artifact_path} responsive at 1920x1080 and 375x812"
        ),
        "build_requirements": (
            "- Use the supplied Vite + React 19 + Tailwind CSS 4 + TypeScript scaffold.\n"
            "- Build with npm install && npm run build using the scaffold's declared "
            "dependencies.\n"
            "- Copy dist/index.html to {artifact_path}; all media and navigation must work and "
            "the browser console must have no errors."
        ),
        "source_isolation_rules": (
            "Do not inspect eval_config.json, the evaluation harness, scorer, ground truth, test "
            "files, scorer installation, or evaluation/ directory."
        ),
        "accessibility_contract": "",
        "security_rules": (
            "\nOriginal-authorship rules (a violating submission is CAPPED AT 0.10):\n"
            "- Reuse binary media and individual design-token values only. Never carry reference "
            "class names into source or data files.\n"
            "- Do not copy raw HTML with innerHTML, dangerouslySetInnerHTML, or DOMParser, and do "
            "not replay a serialized node tree through a generic renderer.\n"
            "- Do not bulk-walk or serialize the page with querySelectorAll, outerHTML, innerHTML, "
            "getComputedStyle, or getBoundingClientRect to harvest DOM, classes, or geometry for "
            "replay. Targeted inspection of a visible property or design-token value is allowed.\n"
            "- Do not embed the original with an iframe or use screenshots as page backgrounds.\n"
        ),
        "platform_guidance": (
            "\nWeb-specific implementation:\n"
            "- Build shared Header, Footer, and Nav components consistently across pages.\n"
            "- Implement each discovered page to match its on-screen reference.\n"
            "- Use path-based client-side routing matching the original URL paths, not hash "
            "routing. Use the existing cn() utility; @/ maps to src/.\n"
        ),
        "process_rule": "",
        "conflict_avoidance": "",
        "final_checklist": "",
    },
}

_PH = re.compile(r"\{(\w+)\}")


class UnfilledPlaceholder(KeyError):
    """A required template or runtime value was not provided."""


def render(platform: str, **runtime: object) -> str:
    """Render one platform's task through the canonical template."""

    selected = (platform or "").lower()
    if selected not in VALUES:
        raise UnfilledPlaceholder(f"no substitution table for platform {platform!r}")
    table = dict(VALUES[selected])
    missing = [key for key in PLACEHOLDERS if key not in table]
    if missing:
        raise UnfilledPlaceholder(f"{selected}: no entry for {', '.join(missing)}")

    try:
        paths = workspace_paths(selected, runtime)
    except WorkspacePathError as exc:
        raise UnfilledPlaceholder(f"{selected}: {exc}") from exc
    substitutions = {**runtime, **paths.substitutions()}

    def _sub_once(text: str, values: dict[str, object]) -> str:
        """Replace placeholders already present in ``text`` without rescanning values."""

        return _PH.sub(
            lambda match: str(values.get(match.group(1), match.group(0))), text
        )

    # Resolve placeholders only in trusted platform blocks. Runtime text such as web page titles is
    # then inserted opaquely, so a literal ``{name}`` in observed content is never reinterpreted.
    resolved: dict[str, str] = {}
    unresolved: set[str] = set()
    for key, value in table.items():
        needed = set(_PH.findall(value))
        unresolved.update(name for name in needed if name not in substitutions)
        resolved[key] = _sub_once(value, substitutions)
    if unresolved:
        raise UnfilledPlaceholder(
            f"{selected}: unresolved after rendering: {', '.join(sorted(unresolved))} "
            "(pass them as runtime kwargs)"
        )
    out = _sub_once(TEMPLATE, resolved)
    # Empty optional blocks must not leave stacks of blank lines, and an empty substitution at
    # the end of a sentence must not leave trailing whitespace behind.
    out = "\n".join(line.rstrip() for line in out.splitlines())
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()
