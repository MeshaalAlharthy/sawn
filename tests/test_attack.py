"""The security note's attack, offline: a compromised model against the naive and the defended setup."""

import redteam


def test_naive_leaks_and_defended_holds(mcp_tools, tmp_path):
    naive, defended = redteam.run("offline", mcp_tools=mcp_tools, out_dir=tmp_path, verbose=False)
    assert naive["data_sent_to_attacker"] and not naive["customer_got_reply"]
    assert not defended["data_sent_to_attacker"]
    assert not defended["attacker_address_in_reply"]
    assert not defended["registered_email_redirected"]
    assert defended["customer_got_reply"]
    assert (tmp_path / "attack_offline.md").exists()
