"""navig vm — local virtual machines on VMware Workstation, built from cloud images.

    navig vm create navig-os-lab --recipe navig-os --repo <signed NAVIG repo folder>
    navig vm start navig-os-lab          # opens the VMware window
    navig vm wait navig-os-lab           # until first-boot provisioning is done
    navig vm snapshot navig-os-lab fresh-install
    navig vm revert navig-os-lab fresh-install

A VM is a folder under the VM root (``plugins.vm.dir``, default ~/Documents/Virtual
Machines) holding the .vmx, its disk, a cloud-init seed, its own SSH key and a small
record. Nothing here touches your personal SSH keys or known_hosts.
"""

from __future__ import annotations

import json as _json
import secrets
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TransferSpeedColumn
from rich.table import Table

from navig_sdk.host import command_name, set_owner_only_file_permissions
from navig_vm.engine import disk, images, seed, store, vmx
from navig_vm.engine.vmware import VMware, VMwareError

CMD = command_name("vm")

console = Console()
err = Console(stderr=True)

vm_app = typer.Typer(
    name="vm",
    help="🖥️  Local virtual machines on VMware Workstation, built from cloud images.",
    no_args_is_help=True,
)

_PROVISIONED = "/var/lib/navig-vm/provisioned"


# ── helpers ────────────────────────────────────────────────────────────────
def _fail(message: str, hint: str = "", code: int = 1) -> None:
    err.print(f"[red]✗ {message}[/red]")
    if hint:
        err.print(f"  [dim]{hint}[/dim]")
    raise typer.Exit(code)


def _vmware() -> VMware:
    try:
        return VMware.locate()
    except VMwareError as exc:
        _fail(str(exc))
        raise  # unreachable; keeps type-checkers honest


def _record(name: str) -> store.VmRecord:
    rec = store.load(name)
    if rec is None:
        _fail(f"No VM named {name!r} under {store.vm_root()}", f"See them with: {CMD} list")
    assert rec is not None
    return rec


def _ssh_argv(rec: store.VmRecord, ip: str, command: list[str] | None = None) -> list[str]:
    ssh = shutil.which("ssh")
    if ssh is None:
        _fail("No ssh client found.", "Windows: Settings → Optional features → OpenSSH Client.")
    argv = [
        str(ssh),
        "-i", str(rec.ssh_key),
        "-o", "IdentitiesOnly=yes",
        # This VM's own known_hosts: a rebuilt VM gets a new host key, and that must not
        # touch (or be refused by) the operator's personal known_hosts.
        "-o", f"UserKnownHostsFile={rec.known_hosts}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=5",
        f"{rec.owner}@{ip}",
    ]
    return argv + (command or [])


def _ip_or_fail(vm: VMware, rec: store.VmRecord, wait: bool = False) -> str:
    if not vm.is_running(rec.vmx):
        _fail(f"{rec.name} is not running.", f"Start it with: {CMD} start {rec.name}")
    ip = vm.guest_ip(rec.vmx, wait=wait, timeout=120 if wait else 20)
    if not ip:
        # VMware Tools is not reporting (first boot reinstalls it); cloud-init already
        # printed the address on the serial console.
        ip = store.ip_from_serial(rec.dir / "serial.log")
    if not ip:
        _fail(
            f"{rec.name} has not reported an IP address yet.",
            "VMware Tools in the guest reports it; on first boot that takes a few minutes.",
        )
    assert ip is not None
    return ip


def _make_ssh_key(path: Path, comment: str) -> str:
    keygen = shutil.which("ssh-keygen")
    if keygen is None:
        _fail("ssh-keygen was not found.", "Windows: Settings → Optional features → OpenSSH Client.")
    subprocess.run(  # noqa: S603 — fixed argv
        [str(keygen), "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
        check=True,
        capture_output=True,
    )
    set_owner_only_file_permissions(path)
    return path.with_suffix(".pub").read_text(encoding="utf-8").strip()


# ── create ─────────────────────────────────────────────────────────────────
@vm_app.command("create")
def create(
    name: str = typer.Argument(..., help="VM name (lowercase letters, digits, dashes)"),
    image: str = typer.Option("debian-13", "--image", help=f"Base image: {', '.join(images.IMAGES)}"),
    recipe: str = typer.Option("plain", "--recipe", help="First-boot recipe: plain | navig-os"),
    repo: str = typer.Option("", "--repo", help="Signed NAVIG apt repo folder to share in (navig-os)"),
    cpus: int = typer.Option(4, "--cpus", min=1, help="Virtual CPUs"),
    memory: int = typer.Option(8192, "--memory", min=1024, help="Memory in MB"),
    disk_gb: int = typer.Option(40, "--disk", min=8, help="Disk size in GB"),
    owner: str = typer.Option(
        "", "--owner", help="The guest's owner account (default: your own user name, lowercased)",
    ),
    keyboard: str = typer.Option("us", "--keyboard", help="Keyboard layout (us, fr, de, …)"),
    start: bool = typer.Option(True, "--start/--no-start", help="Power it on when done"),
    gui: bool = typer.Option(True, "--gui/--headless", help="Open the VMware window when starting"),
    json: bool = typer.Option(False, "--json", help="Machine-readable result"),
) -> None:
    """Build a VM from a cloud image: disk, first-boot seed, its own SSH key."""
    if not store.valid_name(name):
        _fail(f"Invalid name {name!r}", "Use lowercase letters, digits and dashes (max 41).")
    if image not in images.IMAGES:
        _fail(f"Unknown image {image!r}", f"Available: {', '.join(images.IMAGES)}")
    if recipe not in seed.recipe_names():
        _fail(f"Unknown recipe {recipe!r}", f"Available: {', '.join(seed.recipe_names())}")
    owner = owner or store.default_owner()
    if not store.valid_owner(owner):
        _fail(
            f"Invalid owner account {owner!r}",
            "A new, normal account: lowercase, and not one Debian, Ubuntu or navig already uses "
            "(root, operator, staff, navig, …).",
        )

    repo_dir: Path | None = None
    if recipe == "navig-os":
        if not repo:
            _fail(
                "The navig-os recipe installs from a signed NAVIG apt repository.",
                "Build one (apps/os/linux/build-all.sh), copy it to this machine and pass --repo <folder>.",
            )
        from navig_vm._typed_path import resolve_user_path

        repo_dir = resolve_user_path(repo)
        for needed in ("InRelease", "navig-archive-keyring.gpg", "install.sh"):
            if not (repo_dir / needed).is_file():
                _fail(f"{repo_dir} is not a NAVIG repository (missing {needed}).")

    vm = _vmware()
    if vm.vdiskmanager is None:
        _fail("vmware-vdiskmanager was not found next to vmrun — it builds the disk.")

    root = store.vm_root()
    vm_dir = root / name
    if vm_dir.exists():
        _fail(f"{vm_dir} already exists.", f"Pick another name, or remove it with: {CMD} destroy {name}")
    base = images.IMAGES[image]

    # 1. The base image, verified against the publisher's checksums.
    try:
        with Progress(
            TextColumn("[cyan]{task.description}"), BarColumn(), DownloadColumn(),
            TransferSpeedColumn(), console=err, transient=True,
        ) as bar:
            task = bar.add_task(f"{base.id} image", total=None)
            archive = images.fetch(base, lambda done, total: bar.update(task, completed=done, total=total or None))
    except images.ImageError as exc:
        _fail(str(exc))

    vm_dir.mkdir(parents=True)
    rec = store.VmRecord(
        name=name, image=image, recipe=recipe, owner=owner, cpus=cpus, memory_mb=memory,
        disk_gb=disk_gb, created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        password=store.new_password(), repo=str(repo_dir or ""),
    )
    rec.save()  # first, so a half-built folder is still recognisably ours for `destroy`

    try:
        # 2. The disk → growable VMDK at the requested size. A published VMDK converts
        #    as-is (the cached copy is only read); a raw disk gets a descriptor first.
        with console.status("Preparing the disk (extract, convert, grow)…"):
            if base.is_vmdk:
                vm.convert_to_growable(archive, vm_dir / "disk.vmdk")
            else:
                raw = images.extract_disk(archive, base.member, vm_dir / "source.raw")
                src = disk.write_flat_descriptor(raw, vm_dir / "source.vmdk")
                vm.convert_to_growable(src, vm_dir / "disk.vmdk")
                raw.unlink()
                src.unlink()
            vm.expand(vm_dir / "disk.vmdk", disk_gb)

        # 3. First boot: owner account, this VM's own SSH key, the recipe.
        pub = _make_ssh_key(rec.ssh_key, f"navig-vm-{name}")
        user_data = seed.render_recipe(recipe, {
            "hostname": name,
            "owner": owner,
            "password_hash": seed.sha512_crypt(rec.password),
            "ssh_key": pub,
            "keyboard": keyboard,
            "repo_name": "navig-repo",
        })
        seed.write_seed_iso(
            vm_dir / "seed.iso", user_data, seed.meta_data(f"{name}-{secrets.token_hex(4)}", name),
        )

        # 4. The machine.
        spec = vmx.VmSpec(
            name=name, cpus=cpus, memory_mb=memory, guest_os=base.guest_os, shared_folder=repo_dir,
        )
        (vm_dir / f"{name}.vmx").write_text(vmx.render(spec), encoding="utf-8", newline="\n")
    except (OSError, VMwareError, images.ImageError, ValueError, subprocess.CalledProcessError) as exc:
        _fail(f"Could not build {name}: {exc}", f"The partial VM is in {vm_dir}; {CMD} destroy {name} removes it.")

    if start:
        try:
            vm.start(rec.vmx, gui=gui)
        except VMwareError as exc:
            _fail(f"Built, but it would not start: {exc}")

    if json:
        console.print_json(_json.dumps({"name": name, "vmx": str(rec.vmx), "owner": owner, "started": start}))
        return
    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column(no_wrap=True, style="dim")
    table.add_column()
    table.add_row("VM", f"[bold]{name}[/bold] ({image}, {recipe})")
    table.add_row("Folder", str(vm_dir))
    table.add_row("Owner", f"{owner}  ·  password: [bold]{rec.password}[/bold]  (again: {CMD} password {name})")
    table.add_row("Size", f"{cpus} CPU · {memory} MB · {disk_gb} GB")
    table.add_row("State", "[green]● starting[/green]" if start else "[dim]○ off[/dim]")
    console.print(table)
    console.print(
        f"[dim]First boot provisions the machine (packages, then a reboot). "
        f"Follow it with: {CMD} wait {name}[/dim]"
    )


# ── lifecycle ──────────────────────────────────────────────────────────────
@vm_app.command("start")
def start_cmd(
    name: str = typer.Argument(...),
    gui: bool = typer.Option(True, "--gui/--headless", help="Open the VMware window"),
) -> None:
    """Power a VM on."""
    rec = _record(name)
    vm = _vmware()
    if vm.is_running(rec.vmx):
        console.print(f"[green]●[/green] {name} is already running")
        return
    try:
        vm.start(rec.vmx, gui=gui)
    except VMwareError as exc:
        _fail(str(exc))
    console.print(f"[green]✓[/green] {name} started")


@vm_app.command("stop")
def stop_cmd(
    name: str = typer.Argument(...),
    hard: bool = typer.Option(False, "--hard", help="Power off instead of a clean shutdown"),
) -> None:
    """Shut a VM down (cleanly, through the guest, unless --hard)."""
    rec = _record(name)
    vm = _vmware()
    if not vm.is_running(rec.vmx):
        console.print(f"[dim]○ {name} is not running[/dim]")
        return
    try:
        vm.stop(rec.vmx, hard=hard)
    except VMwareError as exc:
        _fail(str(exc), f"Force it with: {CMD} stop {name} --hard")
    console.print(f"[green]✓[/green] {name} stopped")


@vm_app.command("list")
def list_cmd(json: bool = typer.Option(False, "--json", help="Machine-readable output")) -> None:
    """Every VM navig built, and whether it is running."""
    records = store.all_records()
    try:
        running = {str(p).lower() for p in VMware.locate().running()}
    except VMwareError:
        running = set()  # no VMware here: every VM simply reads as off
    rows = [
        {
            "name": r.name, "running": str(r.vmx).lower() in running, "image": r.image,
            "recipe": r.recipe, "cpus": r.cpus, "memory_mb": r.memory_mb, "disk_gb": r.disk_gb,
            "path": str(r.dir),
        }
        for r in records
    ]
    if json:
        console.print_json(_json.dumps(rows))
        return
    if not rows:
        console.print(f"[dim]No VMs yet under {store.vm_root()}. Make one: {CMD} create <name>[/dim]")
        return
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Name", no_wrap=True, style="bold")
    table.add_column("State", no_wrap=True)
    table.add_column("Image · recipe", no_wrap=True)
    table.add_column("Size", no_wrap=True)
    table.add_column("Folder")
    for r in rows:
        state = "[green]● running[/green]" if r["running"] else "[dim]○ off[/dim]"
        table.add_row(
            r["name"], state, f"{r['image']} · {r['recipe']}",
            f"{r['cpus']} CPU · {r['memory_mb'] // 1024} GB · {r['disk_gb']} GB", r["path"],
        )
    console.print(table)
    on = sum(1 for r in rows if r["running"])
    console.print(f"[dim]{on}/{len(rows)} running · start one with {CMD} start <name>[/dim]")


@vm_app.command("ip")
def ip_cmd(
    name: str = typer.Argument(...),
    wait: bool = typer.Option(False, "--wait", help="Wait for the guest to report one"),
) -> None:
    """The VM's IP address (reported by VMware Tools in the guest)."""
    rec = _record(name)
    typer.echo(_ip_or_fail(_vmware(), rec, wait=wait))


def _lf(text: str) -> bytes:
    """Stdin for a Linux guest, as BYTES with bare LF.

    In text mode on Windows, subprocess writes "\\n" as "\\r\\n". sudo stops reading at the
    "\\r" — so the current password still checked out — but chpasswd stored the new one
    WITH a trailing carriage return: a password no keyboard can type (measured, twice).
    """
    return text.replace("\r\n", "\n").encode("utf-8")


@vm_app.command("password")
def password_cmd(
    name: str = typer.Argument(...),
    reset: bool = typer.Option(
        False, "--reset", help="Set a new, easy-to-type password inside the running VM",
    ),
) -> None:
    """The guest owner's password (for the login screen and sudo)."""
    rec = _record(name)
    if not reset:
        typer.echo(rec.password)
        return
    ip = _ip_or_fail(_vmware(), rec)
    old, new = rec.password, store.new_password()

    def accepted(password: str) -> bool:
        """Does sudo in the VM accept this password right now? (-k: never a cached ticket)"""
        probe = subprocess.run(  # noqa: S603 — fixed argv
            _ssh_argv(rec, ip, ["sudo -k -S -p '' true"]),
            input=_lf(f"{password}\n"), capture_output=True, timeout=60, check=False,
        )
        return probe.returncode == 0

    if not accepted(old):
        _fail("The VM does not accept the recorded password, so it cannot be changed from here.",
              f"Revert to a snapshot (navig vm snapshots {name}) or reset it from the VM's console.")
    # The candidate is written down BEFORE it is sent: chpasswd can apply a password and
    # still exit non-zero, and a password that exists only in this process is lost the
    # moment it does (measured — that is how this VM once ended with nobody knowing it).
    rec.pending_password = new
    rec.save()
    # sudo reads the current password from stdin first; the new one travels on stdin too,
    # never on a command line another process could read.
    subprocess.run(  # noqa: S603 — fixed argv
        _ssh_argv(rec, ip, ["sudo -k -S -p '' chpasswd"]),
        input=_lf(f"{old}\n{rec.owner}:{new}\n"), capture_output=True, timeout=60, check=False,
    )
    # chpasswd's exit status is not the answer; what the VM accepts afterwards is.
    if accepted(new):
        rec.password, rec.pending_password = new, ""
        rec.save()
        typer.echo(new)
        return
    if accepted(old):
        rec.pending_password = ""
        rec.save()
        _fail("The VM kept its old password; nothing changed.")
    _fail("The VM accepts neither the old nor the new password.",
          f"Both are recorded in {rec.dir / store.RECORD} (password / pending_password).")


@vm_app.command("ssh", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def ssh_cmd(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Open a shell in the VM as its owner (extra arguments run as a command)."""
    rec = _record(name)
    ip = _ip_or_fail(_vmware(), rec)
    code = subprocess.call(_ssh_argv(rec, ip, list(ctx.args)))  # noqa: S603 — fixed argv
    raise typer.Exit(code)


@vm_app.command("exec")
def exec_cmd(name: str = typer.Argument(...), command: str = typer.Argument(..., help="Shell command")) -> None:
    """Run one command in the VM; its exit status is this command's."""
    rec = _record(name)
    ip = _ip_or_fail(_vmware(), rec)
    code = subprocess.call(_ssh_argv(rec, ip, [command]))  # noqa: S603 — fixed argv
    raise typer.Exit(code)


def _scp_argv(rec: store.VmRecord, ip: str, sources: list[str], dest: str) -> list[str]:
    """scp with the VM's own key and known_hosts — the same isolation `_ssh_argv` keeps."""
    scp = shutil.which("scp")
    if scp is None:
        _fail("No scp client found.", "Windows: Settings → Optional features → OpenSSH Client.")
    return [
        str(scp),
        "-i", str(rec.ssh_key),
        "-o", "IdentitiesOnly=yes",
        "-o", f"UserKnownHostsFile={rec.known_hosts}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=5",
        "-q",
        *sources,
        f"{rec.owner}@{ip}:{dest}",
    ]


@vm_app.command("push")
def push_cmd(
    name: str = typer.Argument(...),
    sources: list[Path] = typer.Argument(..., help="Local files to copy into the VM"),
    dest: str = typer.Option("/tmp/", "--to", help="Destination path in the VM (a directory ends with /)"),
) -> None:
    """Copy local files into the VM (scp, as its owner); its exit status is this command's."""
    rec = _record(name)
    missing = [str(s) for s in sources if not s.is_file()]
    if missing:
        _fail(f"Not a file: {', '.join(missing)}")
    ip = _ip_or_fail(_vmware(), rec)
    code = subprocess.call(_scp_argv(rec, ip, [str(s) for s in sources], dest))  # noqa: S603 — fixed argv
    raise typer.Exit(code)


@vm_app.command("wait")
def wait_cmd(
    name: str = typer.Argument(...),
    timeout: int = typer.Option(1800, "--timeout", help="Give up after this many seconds"),
) -> None:
    """Wait until first-boot provisioning has finished (and, for navig-os, the reboot)."""
    rec = _record(name)
    vm = _vmware()
    check = f"test -f {_PROVISIONED}"
    if rec.recipe == "navig-os":
        # The recipe reboots into the login screen; graphical.target is only reached after it.
        check += " && systemctl is-active --quiet graphical.target"
    deadline = time.monotonic() + timeout
    with console.status(f"Waiting for {name} to finish provisioning…") as status:
        while time.monotonic() < deadline:
            ip = None
            if vm.is_running(rec.vmx):
                ip = vm.guest_ip(rec.vmx, wait=False, timeout=20) or store.ip_from_serial(rec.dir / "serial.log")
            if ip:
                status.update(f"Waiting for {name} ({ip}) to finish provisioning…")
                probe = subprocess.run(  # noqa: S603 — fixed argv
                    _ssh_argv(rec, ip, [check]), capture_output=True, timeout=30, check=False,
                )
                if probe.returncode == 0:
                    console.print(f"[green]✓[/green] {name} is ready at {ip}")
                    return
            time.sleep(10)
    _fail(f"{name} was not ready within {timeout}s.", f"Look inside: {CMD} ssh {name} -- sudo cloud-init status --long")


@vm_app.command("console")
def console_cmd(
    name: str = typer.Argument(...),
    lines: int = typer.Option(40, "--lines", "-n", help="How many of the last lines"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep printing new lines (Ctrl-C to stop)"),
) -> None:
    """The VM's serial console: kernel, systemd and cloud-init output, as text."""
    rec = _record(name)
    log = rec.dir / "serial.log"
    if not log.is_file():
        _fail(f"{name} has no serial log yet.", "It appears once the VM has been started.")
    with log.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh.readlines()[-lines:]:
            typer.echo(line.rstrip("\r\n"))
        if not follow:
            return
        try:
            while True:
                line = fh.readline()
                if line:
                    typer.echo(line.rstrip("\r\n"))
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            return


# ── snapshots ──────────────────────────────────────────────────────────────
@vm_app.command("snapshot")
def snapshot_cmd(name: str = typer.Argument(...), snap: str = typer.Argument(..., help="Snapshot name")) -> None:
    """Save the VM's current state under a name."""
    rec = _record(name)
    try:
        _vmware().snapshot(rec.vmx, snap)
    except VMwareError as exc:
        _fail(str(exc))
    console.print(f"[green]✓[/green] snapshot {snap!r} of {name}")


@vm_app.command("revert")
def revert_cmd(name: str = typer.Argument(...), snap: str = typer.Argument(..., help="Snapshot name")) -> None:
    """Go back to a snapshot (the VM is powered off afterwards; start it again)."""
    rec = _record(name)
    try:
        _vmware().revert(rec.vmx, snap)
    except VMwareError as exc:
        _fail(str(exc), f"List them with: {CMD} snapshots {name}")
    console.print(f"[green]✓[/green] {name} reverted to {snap!r}")


@vm_app.command("snapshots")
def snapshots_cmd(name: str = typer.Argument(...)) -> None:
    """The VM's snapshots."""
    rec = _record(name)
    try:
        names = _vmware().snapshots(rec.vmx)
    except VMwareError as exc:
        _fail(str(exc))
    for n in names:
        typer.echo(n)
    if not names:
        console.print(f"[dim]No snapshots. Take one: {CMD} snapshot {name} <snapshot>[/dim]")


@vm_app.command("screenshot")
def screenshot_cmd(
    name: str = typer.Argument(...),
    out: Path = typer.Option(Path("vm-screen.png"), "--out", "-o", help="PNG to write"),
) -> None:
    """Save a PNG of the VM's screen (needs VMware Tools running in the guest)."""
    rec = _record(name)
    target = out.expanduser().resolve()
    try:
        _vmware().capture_screen(rec.vmx, target, rec.owner, rec.password)
    except VMwareError as exc:
        _fail(str(exc))
    console.print(f"[green]✓[/green] {target}")


# ── destroy ────────────────────────────────────────────────────────────────
@vm_app.command("destroy")
def destroy_cmd(
    name: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask"),
) -> None:
    """Delete a VM and its folder. Only ever a folder navig created under the VM root."""
    rec = _record(name)
    if not store.is_ours(rec.dir):
        _fail(f"{rec.dir} is not a navig VM folder under {store.vm_root()} — not deleting it.")
    if not yes and not typer.confirm(f"Delete {name} and everything in {rec.dir}?", default=False):
        raise typer.Exit(1)
    vm = _vmware()
    try:
        if vm.is_running(rec.vmx):
            vm.stop(rec.vmx, hard=True)
        if rec.vmx.is_file():
            vm.delete_vm(rec.vmx)
    except VMwareError as exc:
        err.print(f"[yellow]VMware said: {exc} — removing the folder anyway[/yellow]")
    if rec.dir.exists():
        shutil.rmtree(rec.dir)
    console.print(f"[green]✓[/green] {name} deleted")
