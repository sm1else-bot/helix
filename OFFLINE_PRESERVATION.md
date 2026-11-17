# Private offline preservation (in progress)

The local capture archive is `project helix/.offline-archive/`, which is excluded from Git. It may contain sponsor data, local environment backups, imagery, and private records. Do not publish or share it without establishing the applicable retention and redistribution rights. The sponsor kit, credentials, imagery, and captured investigations must not be added to this repository.

`project helix/offline_archive.py` provides atomic JSON checkpoint writes and SHA-256 indexing of scenario files. `python3 "project helix/verify_offline_demos.py"` checks scenario manifests and declared file hashes without network access. It reports partial scenarios as incomplete. A passing checksum check alone is not proof that a scenario can be replayed offline or that its content may be redistributed.

Back up existing files before collecting data. Keep original provider responses and generated outputs separate from derived indexes; record the property, listing, and space identities, source and retrieval time, incomplete stages, and any rights restrictions. Index a scenario from `project helix` with:

```sh
python3 -c 'from pathlib import Path; from offline_archive import index_scenario; index_scenario(Path(".offline-archive/scenarios/SCENARIO_ID"))'
python3 verify_offline_demos.py
```

The private archive and its backups must be restored separately from Git. Preserve the archive directory and its permissions when copying it to another local machine; verify hashes after restoring. A manifest must never be marked `VERIFIED` merely because its files are present: complete identity, stage, media, and offline browser checks are also required.

`project helix/offline_panorama_faces.html` is a local viewer for six previously saved faces from one Hardy Road panorama. Copy it as `index.html` into the private directory containing `skybox_face_0.jpg` through `skybox_face_5.jpg` and open it locally. It does not establish face orientation, reconstruct a spherical panorama, or support navigation between locations. The six images and their source manifest are private artifacts, not part of the repository.

The original `HELIX_DEMO_REPLAY` mode remains unchanged. A strict offline replay mode, offline scenario selection, a navigable panorama viewer, and browser network audit have **not** yet been implemented. The archive verifier does not perform these checks. Hosted Matterport links are not offline 3D models. A scenario with no eligible Stage 3 shortlist cannot proceed through the existing business-selection handoff without changing its inputs or resolving the actual evidence blockers.
