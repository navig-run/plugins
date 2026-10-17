"""The cloud-init seed: password hashing, recipe rendering, the CIDATA ISO."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import yaml

from navig_vm.engine import seed

# Reference hashes from an independent implementation (`openssl passwd -6 -salt …`).
# The first is also the published SHA-crypt test vector.
VECTORS = [
    ("Hello world!", "saltstring",
     "$6$saltstring$svn8UoSVapNtMuq1ukKS4tPQd8iKwSMHWjl/O817G3uBnIFNjnQJuesI68u4OTLiBFdcbYEdFCoEOfaS35inz1"),
    ("correct horse battery staple", "Xy7./abcDEF12345",
     "$6$Xy7./abcDEF12345$FlYkvj7czS8wdLc4pjVTcwAZHxrOK59C57o30i.3qygoGUcJV2QyUij10rgC0Yp3bjSlmCrlLDh1BuIU8UGbM."),
    ("x", "short",
     "$6$short$QyVoKWqPNl6RNenmBA6a/vLfaqSQjTh9uln9bU2ulhek2oiVdUAoCfuszVp.1rkl8XCLSBKCMmlydCXQ0ITpm/"),
    # Longer than one SHA-512 block: exercises the 64-byte loops.
    ("p" * 130, "0123456789abcdef",
     "$6$0123456789abcdef$JECTHj31aYreqMf0wEeM.DHP.hYYoZmiefREarhSh8Ty438I81hOK3RY99RrvHd9H/.tGv.Wfk7gkyZbBpj/S/"),
]


@pytest.mark.parametrize(("password", "salt", "expected"), VECTORS)
def test_sha512_crypt_matches_openssl(password: str, salt: str, expected: str) -> None:
    assert seed.sha512_crypt(password, salt) == expected


def test_a_fresh_salt_every_time() -> None:
    a, b = seed.sha512_crypt("same"), seed.sha512_crypt("same")
    assert a != b and a.startswith("$6$") and b.startswith("$6$")


VALUES = {
    "hostname": "lab",
    "owner": "navig",
    "password_hash": seed.sha512_crypt("pw", "abcdefgh"),
    "ssh_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample navig-vm-lab",
    "keyboard": "fr",
    "repo_name": "navig-repo",
}


@pytest.mark.parametrize("recipe", ["plain", "navig-os"])
def test_recipes_render_to_valid_cloud_config(recipe: str) -> None:
    text = seed.render_recipe(recipe, VALUES)
    assert text.startswith("#cloud-config\n")
    data = yaml.safe_load(text)
    user = data["users"][0]
    assert user["name"] == "navig"
    # The hash survives YAML intact ($ signs and all) — a mangled hash locks the owner out.
    assert user["passwd"] == VALUES["password_hash"]
    assert user["ssh_authorized_keys"] == [VALUES["ssh_key"]]
    keyboard = [f for f in data["write_files"] if f["path"] == "/etc/default/keyboard"]
    assert keyboard and 'XKBLAYOUT="fr"' in keyboard[0]["content"]
    # The `keyboard` module fails on the Debian cloud image (seen on a real first boot).
    assert "keyboard" not in data
    assert "${" not in text


def test_the_navig_os_recipe_installs_through_the_shared_installer() -> None:
    data = yaml.safe_load(seed.render_recipe("navig-os", VALUES))
    cmds = [c if isinstance(c, list) else [c] for c in data["runcmd"]]
    install = [c for c in cmds if "/mnt/navig-repo/install.sh" in c]
    assert install, "the recipe must install through apps/os/linux/install.sh"
    assert "--owner" in install[0] and "navig" in install[0]
    assert data["power_state"]["mode"] == "reboot"
    # The `mounts` module runs before packages exist (no vmhgfs-fuse yet) and fails;
    # the recipe writes fstab in runcmd instead, after open-vm-tools is installed.
    assert "mounts" not in data
    fstab = [c for c in cmds if any("/etc/fstab" in str(x) for x in c)]
    assert fstab and "fuse.vmhgfs-fuse" in " ".join(map(str, fstab[0]))


def test_a_missing_value_is_an_error_not_a_half_filled_config() -> None:
    values = dict(VALUES)
    del values["ssh_key"]
    with pytest.raises(ValueError, match="ssh_key"):
        seed.render_recipe("plain", values)


def test_unknown_recipe() -> None:
    with pytest.raises(ValueError, match="unknown recipe"):
        seed.render_recipe("nope", VALUES)


def test_seed_iso_is_labelled_cidata_with_exact_names(tmp_path: Path) -> None:
    pycdlib = pytest.importorskip("pycdlib")
    user_data = seed.render_recipe("plain", VALUES)
    meta = seed.meta_data("lab-1234", "lab")
    iso_path = seed.write_seed_iso(tmp_path / "seed.iso", user_data, meta)

    iso = pycdlib.PyCdlib()
    iso.open(str(iso_path))
    try:
        assert iso.pvd.volume_identifier.decode().strip() == "CIDATA"
        for name, body in (("user-data", user_data), ("meta-data", meta)):
            buf = io.BytesIO()
            iso.get_file_from_iso_fp(buf, rr_path=f"/{name}")
            assert buf.getvalue().decode("utf-8") == body
            buf = io.BytesIO()
            iso.get_file_from_iso_fp(buf, joliet_path=f"/{name}")
            assert buf.getvalue().decode("utf-8") == body
    finally:
        iso.close()
    assert not (tmp_path / "seed.iso.part").exists()
