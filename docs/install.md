# Installing Annex on a builder's machine

For anyone who builds apps on an Annex instance: Kaldi's people on Kaldi's instance, a client's people on theirs.
The CLI, the MCP server and the Claude Code skill are the same for everyone; one command points them at **your**
instance, and they stay there until you point them somewhere else (D119). Nothing here changes the Annex server.

## What you get from your platform team

- **An SSH host alias and a key** for the server, as a block for `~/.ssh/config`, e.g.:
  ```
  Host annex-kaldi
    HostName <the server's address>
    User annex
    IdentityFile ~/.ssh/annex_kaldi_ed25519
    IdentitiesOnly yes
    ServerAliveInterval 30
  ```
  That key lets you deploy, and it is root-equivalent on the server: keep it to yourself.
- **An Annex account** (an email invitation) to open the apps in the browser, and your login, which is the `owner`
  of the apps you build.
- The data directory, only if it isn't `/srv/annex`.

## Install

Needs `git`, `ssh` and [`uv`](https://docs.astral.sh/uv/) (it fetches the right Python itself), and Claude Code for
the agent part.

1. **The toolkit.** Clone the Annex repository somewhere that stays (the MCP server and the skill point into it):
   ```bash
   git clone <annex repository URL> ~/annex && cd ~/annex
   ```
2. **SSH.** Put the block in `~/.ssh/config` and the key in `~/.ssh`, then check (no prompt, no output):
   ```bash
   ssh annex-kaldi true
   ```
3. **Point Annex at your instance.** It asks the server over SSH and remembers it in `~/.config/annex/instance`:
   ```bash
   ./annex connect annex-kaldi
   ```
   It answers with the instance's home page (`Annex now points at https://home...`). `./annex connect` alone shows
   where it points; run it again with another alias to switch. `--data-dir` if your team gave you one.
4. **Claude Code: the MCP server.** Always under the name `annex`; it goes wherever step 3 pointed:
   ```bash
   claude mcp add --scope user annex -- uv run --quiet --project "$(pwd)/toolkit/mcp" annex-mcp
   ```
   Check with `claude mcp list` (it should say "Connected").
5. **Claude Code: the skill.** A link, so `git pull` updates it:
   ```bash
   mkdir -p ~/.claude/skills && ln -s "$(pwd)/toolkit/skill" ~/.claude/skills/annex
   ```

## Check

```bash
./annex contract | tail -20
```
The end of the "Gateway views" section names your instance's data views (or says it has none). Then, in an empty
folder, ask Claude Code for a small internal tool "on Annex": it reads the contract, starts from a template,
deploys and gives you the URL.

## Keeping it current

`git pull` in the clone. If a deploy says your CLI is older than the platform ("update your checkout"), that is
the fix; if it says the CLI is newer, the platform team hasn't pushed yet.

## Undo

```bash
claude mcp remove --scope user annex
```
```bash
rm ~/.claude/skills/annex ~/.config/annex/instance
```
Then delete the clone, the SSH block and the key.

## For the platform team

The repo's scripts (`scripts/push.sh`, the suites) don't use the pointer: they pick their lane with `ANNEX_ENV`
(unset means `vm`, the repo's `.env`), and so does `./annex` when `ANNEX_ENV` is set (`docs/operations.md`, "Two
lanes"). An operator's own `./annex` and MCP server follow the pointer like everyone else's.
