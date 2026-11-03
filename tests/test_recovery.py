import pytest
from cryptography.fernet import InvalidToken
from mhphotos_lightroom_mcp.recovery import FILES, backup, restore


def test_encrypted_roundtrip_preserves_secrets_and_rejects_overwrite(tmp_path):
    home, restored = tmp_path / 'home', tmp_path / 'restored'
    home.mkdir()
    restored.mkdir()
    for name in FILES:
        p = home / name
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        p.write_bytes(('synthetic-' + name).encode())
        p.chmod(0o600)
    key, archive = tmp_path / 'key', tmp_path / 'backup.enc'
    assert backup(home, archive, key) == 4
    assert b'synthetic-' not in archive.read_bytes()
    assert restore(restored, archive, key) == 4
    assert restore(restored, archive, key) == 4
    for name in FILES:
        assert (restored / name).read_bytes() == (home / name).read_bytes()
    target = restored / FILES[0]
    target.write_bytes(b'changed')
    with pytest.raises(ValueError, match='differs'):
        restore(restored, archive, key)
    assert target.read_bytes() == b'changed'
    bad = archive.read_bytes()
    archive.write_bytes(bad[:20] + b'x' + bad[21:])
    with pytest.raises(InvalidToken):
        restore(tmp_path / 'other', archive, key)
    assert not (tmp_path / 'other').exists()
