# Credits

`retro-plus` extends [`retro`](../retro/SKILL.md): its categories, steps and reference sections are carried over and remain under this repo's MIT license.

The cross-session mechanisms (environment facts learned from failures that repeat across sessions, pruning of tools and skills that go unused, and detection of turns stuck on the same failure) are ideas from [TokenEconomizer](https://github.com/microsoft/TokenEconomizer) by Microsoft Research and UC Berkeley (Apache-2.0; paper arXiv 2610.22732). No code is reused. `retro-plus` adapts them to a propose-only retrospective: it reads saved logs after the fact, needs no hooks, and changes nothing without the user's approval.
