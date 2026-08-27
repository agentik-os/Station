# Station operations

## Health

```bash
station doctor
station status
```

## Discord

```bash
sudo station discord token list
sudo station discord token check TARGET
sudo station discord token rotate TARGET
```

Rotation uses hidden input and validates the Discord bot before writing. Application creation, privileged intents and OAuth installation remain owner-controlled.

## Backup and recovery

```bash
sudo station backup
sudo station backup --full-state
sudo station rollback
```

Recovery archives are local secrets. Keep them encrypted/off-host and never attach them to Discord or GitHub.

## Updating

```bash
sudo station update --ref main
```

Production deployments should use a Station tag or 40-character commit. Update AGK-TUI/Hermes pins only after CI and a clean-host install test.

## Troubleshooting

1. `station doctor`
2. inspect the exact service, not every gateway;
3. preserve profile isolation and existing sessions;
4. restart only the affected unit;
5. verify the real Discord/Portal flow;
6. roll back if the release gate fails.
