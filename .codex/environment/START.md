Use the existing /workspace/Folder_Organizer checkout. Cloud tasks are already isolated; do not make another checkout/worktree unless requested. Preserve application source, tests, dependency declarations, lockfiles, existing files, and local commits during environment setup. Use polite Korean with this user. Do not publish without a new explicit user instruction.

Before project development commands:
    . /workspace/.folder-organizer-tools/activate.sh
    cd /workspace/Folder_Organizer

The retained toolchain includes Python 3.12, the pinned runtime/dev/bundle extras, setuptools 80.9.0, wheel 0.45.1, pip 26.3, and user-local Rust 1.99.1 with Cargo/rustfmt/Clippy and a four-job limit. Linux Cargo.lock dependencies including optional GUI dependencies are cached. The .venv, writable caches, and inherited proxy/TLS trust are configured. Use cargo +1.99.1 with the subcommand before --manifest-path rust-app/Cargo.toml and --locked. GUI checks and Windows packaging run only for a requested task.

Do not automatically launch the repository application, run builds/tests, take application screenshots, or publish merely to start the environment. Follow docs/RELEASE_POLICY.md: skip passed and unchanged-feature suites, run only relevant new/changed/failed cases, and capture application screens only when the user requests them. Necessary VM installation/login diagnostics are separate environment troubleshooting; do not turn them into release gates. Previously passed dependency checks are not an application regression suite. A fresh-task restoration is not independently verified.

Preserve inherited proxy and CA settings; inspect actual environment policy/readiness before network work. HTTP/HTTPS networking is unrestricted in the selected published environment. Do not disable TLS/checksum validation, rewrite policy JSON, or install a separate VPN. No external Windows host or VPN is required for the local Windows VM.

Cloud-local Windows is the user's chosen Windows workflow, superseding the earlier external-PC connection. Its files are /workspace/.folder-organizer-tools/windows-vm. It uses QEMU TCG (no /dev/kvm), 2 vCPU, 8 GiB RAM, a sparse 64 GiB disk, UEFI Secure Boot, and TPM 2.0. Boot/login can be slow under software emulation. The media is official Microsoft Korean Windows 11 Enterprise Evaluation with a verified Microsoft SHA-256; it is an evaluation license, not a purchased Windows license. Use normal Microsoft activation and respect its term. Never bypass activation or change the clock to extend it.

Start/resume the existing installation:
    python3 /workspace/.folder-organizer-tools/windows-vm/vm.py start
    python3 /workspace/.folder-organizer-tools/windows-vm/vm.py status

Never run prepare.py on an existing disk or replay Autounattend installation to refresh tools. installed=false can mean Bootstrap is incomplete; it does not prove Windows needs reinstalling. Readiness is reported through optional COM1 serial, the small FOLOGS FAT metadata disk, and retained guest metadata; confirm a real RDP negotiation/authentication before claiming connectivity. RDP is forwarded ONLY from host 127.0.0.1:13389 to this VM's 3389. Guest HTTP/HTTPS uses the existing managed proxy via a loopback relay. This is not an external-host RDP connection and needs no user-supplied Windows IP or managed VPN grant.

The Developer administrator password is generated privately in state/credentials.json. NEVER print it, put it in process arguments, copy it into configuration, or dump private unattended media/hives. The RDP helper uses an inherited FD and a pinned guest certificate. keyboard.py sign-in may submit the private credential only after checking the Developer password field is focused. Its slower key timings were needed for reliable software-emulated input. Do not guess/reset credentials merely because a key transmission failed.

Desktop use:
1. Prefer the managed shared desktop if tools are actually available. The latest environment declares remote_desktop enabled/on demand, but setup-time tool discovery exposed no desktop js/open_in_codex tool and cloud-agent-desktop reported unavailable. Declared capability is not proof a viewer works. Use the cloud-desktop skill's supported tools when offered; do not invent a viewer URL. The local :99 session below does not automatically appear in a separate platform desktop.
2. To use the retained authenticated Xorg/XFCE display:
       python3 /workspace/.folder-organizer-tools/desktop/desktop.py start
       . /workspace/.folder-organizer-tools/desktop/activate.sh
   It uses owner-only Xauthority under /tmp and binds no external X11 TCP port. Qt XCB, XFCE, session D-Bus, and GTK file-chooser portal were verified previously. Use the execution tool's normal approved host context for desktop/QMP sockets; sandbox reachability failure does not prove the host session is down.
3. Once Windows metadata and service are ready:
       python3 /workspace/.folder-organizer-tools/windows-vm/vm.py auth-check
       python3 /workspace/.folder-organizer-tools/windows-vm/vm.py desktop
   desktop opens FreeRDP inside the prepared local Linux desktop. A working local window is not proof of a connected user-facing shared viewer. If a valid platform display is available, use its supported session rather than promising a public endpoint.
4. To stop Windows, use vm.py stop and allow normal guest shutdown to finish. The first 50-second wait can expire on slow software emulation; inspect current state and continue waiting. Do not force-stop the normal installed guest merely because that wait elapsed. Stop the managed Linux session separately with desktop.py stop if needed. Running processes and /tmp sockets do not survive environment snapshots.

Important retained recovery state: current state/windows.qcow2 has state/windows.pre-logon-repair.qcow2 as a required backing file. KEEP BOTH. The original installed disk was preserved while Windows PE repaired a defaultuser0 automatic-logon loop. Do not delete, move, flatten, replace, or reinstall either disk as routine setup. Windows PE recovery used separate recovery-nvram.fd and recovery.iso; maintenance boot must NEVER attach setup.iso containing unattended disk-wiping instructions. Normal starts use the retained normal nvram.fd and boot the existing installation.

The combined repeatable setup is saved in install_script and /workspace/.folder-organizer-tools/install-with-windows-vm.sh. Run it only when installation/refresh is needed. It retains an existing Windows disk and prepares a fresh disk only when none exists; it does not launch Windows or application suites. Do not claim a new task was independently restored merely because the configuration saved successfully.

Before any user-authorized environment publication/snapshot, shut Windows down cleanly. Do not publish a running VM disk. Saving setup instructions as a draft does not publish or apply that draft; preserve unrelated configuration and repositories when updating it.

CPU compatibility: use Haswell-v2,pmu=off,vmx=off for this TCG VM. The previous max CPU exposed unsupported RDPMC reads and caused confirmed Windows kernel crashes. PMU/VMX/SVM are off. Preserve this CPU setting. The experimental COM1 guest agent is disabled and is not a working management route.

Current confirmed Windows state (2026-11-16): OOBE finished successfully (native log exit0, IMAGE_STATE_COMPLETE at18:14:56 UTC); Bootstrap completed and FAT ready.json reports build26300, IP10.0.2.15 and the guest SHA256 RDP certificate. WinHTTP and full-machine WinINET proxy10.0.2.2:18080 and the managed root CA are configured. Native HTTPS returned200 with per-request curl --ssl-revoke-best-effort because the managed certificate has no revocation method; chain and hostname verification remain enabled. Do not globally weaken TLS/revocation checks.

Desktop readiness is NOT confirmed. Earlier Developer NLA/RDP authentication passed, but the latest pinned auth failed at RDP session activation (exit131); a longer-timeout graphical client connected to a black screen and was then disconnected by Windows. Explorer crashed in twinui.pcshell.dll with0x80270233. The console shows a waiting screen. vm.py now allows120s connection setup and bounds auth checks at150s; status reports configured_waiting_for_rdp rather than assuming boot. Inspect the actual current screen and logs before declaring a working desktop. No application was launched.

Normal evaluation activation FAILED: Security-SPP reported0x8004FE93 and HTTP503 from activation-v2.sls.microsoft.com, and a host request to the same endpoint returned503. No license workaround, rearm or clock change was used. Do not claim the evaluation is activated. Preserve this blocker until a normal Microsoft activation succeeds.

Viewer limitation: this environment settings chat exposes no desktop js/CUA/open_in_codex/open_cloud_preview tool, and the user's shown panel has no Desktop/+ menu. Do not repeat earlier unsupported menu guidance, invent a viewer URL, or treat local Xorg/FreeRDP windows as user-visible. User-facing shared viewing is blocked independently of Windows initialization.

Use native Windows reads and FAT metadata for provisioning state; raw reads of the live encrypted NTFS volume can be stale. Never print private Cached-unattend.xml or credentials. Unused diagnostic disk copies were deleted after verifying the active chain; retain BOTH active windows.qcow2 and required windows.pre-logon-repair.qcow2. No publication occurred; save only as a draft. Fresh-task restoration remains unverified.

Latest runtime action: normal ACPI shutdown completed and the retained Windows disk was restarted without force-stop or reinstall. The guest is booting; usable desktop recovery remains unverified. vm.py stop now waits for its proxy/TPM processes to exit before removing records, preventing an observed immediate-restart port release race. Check actual QMP/process and Windows UI state before claiming readiness. The activation HTTP503 body says upstream connection failure at the managed proxy; do not bypass that proxy.
