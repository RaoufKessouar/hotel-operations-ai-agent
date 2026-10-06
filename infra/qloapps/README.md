# Local QloApps environment

This directory runs a reproducible QloApps 1.7.x laboratory environment at `http://127.0.0.1:8088`. QloApps is the open-source hotel management application used as the operational source of truth.

MySQL and SSH are not exposed to the host. Application and database state are stored in named Docker volumes.

## 1. Configuration

Copy the example environment file:

```powershell
Copy-Item .env.example .env
```

Replace every example password before starting the environment. The file is ignored by Git.

## 2. Start QloApps

From this directory:

```powershell
docker compose --env-file .env up -d
```

During first-time installation, use:

- database host: `127.0.0.1`;
- database name: `QLOAPPS_DB_NAME` from `.env`;
- database user: `root`;
- database password: `QLOAPPS_DB_ROOT_PASSWORD`;
- local URL: `http://127.0.0.1:8088`.

The local administrator credentials and back-office path are also defined in `.env`. Use synthetic data only.

## 3. Configure bounded read access

The read credential is restricted to the resources required by the adapter. Availability lookup requires a bounded `POST` permission on QloApps' availability resource; other reads use `GET` or `HEAD`.

```powershell
docker exec agentpms-qloapps php /opt/agentpms/bootstrap_read_api.php
```

## 4. Seed synthetic scenarios

```powershell
.\seed_demo.ps1
```

The seed is idempotent. It creates synthetic bookings and a customer request, configures realistic room prices between EUR 120 and EUR 300 per night, and sets breakfast to EUR 15 per guest and night.

See [demo_scenarios.md](demo_scenarios.md) for the generated records.

## 5. Configure bounded write access

The write credential is separate from the read credential. It is limited to the booking, room-assignment and customer-message resources required by the agent. It has no deletion, payment or configuration permission.

```powershell
docker exec agentpms-qloapps php /opt/agentpms/bootstrap_write_api.php
```

Store the generated values only in the root `.env` file. Without the restricted write credential, state-changing tools are not registered.

## Data integrity controls

- Missing contact details are represented by unique internal placeholders and are never presented as real customer contact information.
- Multi-night pricing is recalculated and compared with the value read back from QloApps.
- Service prices and tax rules are read from the catalogue.
- A write is successful only when a second read confirms the expected state.
- A verified rollback is attempted when a supported service write produces an unexpected financial result.

The adapter rejects unsupported complex records when it cannot guarantee preservation of every existing room, discount or service line.

