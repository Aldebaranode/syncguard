# Manual recovery runbook (baseline drill, ruling D3)

This procedure measures how long a human operator needs to make validator 48DC2183 sign again after its primary node dies, without syncguard. The harness command `python -m harness run drill --session <S> --operator "<name>"` kills `validator1-node`, raises an alert when 48DC2183 misses a committed height, and records every command typed at its prompt `command (or 'done')>` with host timestamps. Each command runs on its own through `/bin/sh -c`, so every command below spells out full paths.

## Before the drill

- Stop both syncguard processes. The harness refuses to start while one runs.
- The harness checks that validator1 holds 48DC2183, validator2 holds a mock key, and 48DC2183 is signing.
- Read this runbook once before typing `ready`; keep it open in a second window.
- Jailing: with `signed_blocks_window` 200 and `min_signed_per_window` 0.05 the validator is jailed after 190 missed blocks. Blocks were about 3 s apart in the 2026-10-05 smoke test (heights 126 to 129), so 190 blocks is roughly 9.5 minutes. If 190 blocks pass before 48DC2183 signs again, the validator is jailed. Unjailing is outside the scope of this drill: stop, type `done`, and report the jailing instead of continuing with unjail transactions. Session end records the signing info, jail included.

## Timed steps

1. Confirm the primary is down. Expected `exited`. If it shows `running`, run `docker stop validator1-node` first: a running primary with the validator key would sign alongside the backup.

   ```
   docker inspect -f '{{.State.Status}}' validator1-node
   ```

2. Keep the backup's own key aside.

   ```
   cp /Users/heed/Projects/story/story-localnet/config/story/validator2/story/config/priv_validator_key.json /Users/heed/Projects/story/story-localnet/config/story/validator2/story/config/priv_validator_key.json.drill-backup
   ```

3. Copy the validator key onto the backup.

   ```
   cp /Users/heed/Projects/story/story-localnet/config/story/validator1/story/priv_validator_key.json /Users/heed/Projects/story/story-localnet/config/story/validator2/story/config/priv_validator_key.json
   ```

4. Carry the primary's last signing state to the backup, so the backup never signs at or below a height the primary signed.

   ```
   cp /Users/heed/Projects/story/story-localnet/config/story/validator1/story/data/priv_validator_state.json /Users/heed/Projects/story/story-localnet/config/story/validator2/story/data/priv_validator_state.json
   ```

5. Restart the backup node.

   ```
   docker restart validator2-node
   ```

6. Confirm the backup loaded the validator key. Repeat until it answers (the node sleeps 10 s before it starts); the answer must contain `"address":"48DC218393FCEEF56A37D963B804FAB92C62CA9D"` under `validator_info`.

   ```
   curl -s -m 2 localhost:22657/status
   ```

7. Type `done`. The harness waits for the first 48DC2183 signature in `/commit` and records it as the end of the recovery.

## After the drill (not timed)

Run these at a normal shell to restore the layout syncguard expects. When the restore is complete, type `restored` in the harness terminal; the harness keeps the dual-signer monitor running until you do. The order is break-before-make: the backup stops signing before the primary starts.

```
cp /Users/heed/Projects/story/story-localnet/config/story/validator2/story/config/priv_validator_key.json.drill-backup /Users/heed/Projects/story/story-localnet/config/story/validator2/story/config/priv_validator_key.json
docker restart validator2-node
curl -s -m 2 localhost:22657/status
```

The node sleeps 10 s before it starts, so repeat the last command until it answers. Once it answers it must show a `validator_info.address` other than 48DC2183. Jika alamat yang tampil 48DC218393FCEEF56A37D963B804FAB92C62CA9D, jangan jalankan validator1-node; hentikan dan laporkan. Otherwise continue:

```
cp /Users/heed/Projects/story/story-localnet/config/story/validator2/story/data/priv_validator_state.json /Users/heed/Projects/story/story-localnet/config/story/validator1/story/data/priv_validator_state.json
docker start validator1-node
rm /Users/heed/Projects/story/story-localnet/config/story/validator2/story/config/priv_validator_key.json.drill-backup
```

Check the layout before starting syncguard again: `validator1/story/priv_validator_key.json` must hold 48DC2183 and `validator2/story/config/priv_validator_key.json` must not.
