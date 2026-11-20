# Ashur — notes for Claude Code

## Context from Sergio's claude.ai conversations

Sergio's claude.ai history about this project lives in the Obsidian vault Ashur generates, in its `claude/` folder:

- On the Mac (Syncthing copy of the server's vault): `~/Ashur-vault/claude/`
  - `claude/conversaciones/` — one note per conversation (date, title, project, people, full transcript)
  - `claude/proyectos/` — one note per claude.ai project, with links to its conversations
  - `claude/memoria/` — the memory claude.ai keeps
- On the server: `$VAULTS_ROOT/Ashur/claude/` (default `/DATA/AppData/ashur/vaults/Ashur/claude/`)

Before a task that depends on an earlier decision ("the encargos design", "what we chose for Casa"), search there first
(`grep -ril "<words>" ~/Ashur-vault/claude/`) and read the matching notes. If the folder does not exist on this machine
(a cloud session, say), skip this and work from the repo, its `docs/` and `CHANGELOG.md`.

The vault is generated one way (Ashur's database, Notion, the claude.ai export, Sergio's files → vault): never write,
move or delete anything in it. The vault's other folders (`ashur/`, `notion/`, `sergio/`) are not context for code
work unless Sergio asks; `sergio/` holds summaries of his own files.
