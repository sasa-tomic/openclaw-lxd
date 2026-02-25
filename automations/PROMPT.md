I just subscribed for a Premium+ X/Twitter account, as recommended by AI in decent-cloud-twitter-plan.md
I want EVERYTHING that we have related to twitter/X to be MAXIMALLY and COMPLETELY aligned with the @decent-cloud-twitter-plan.md
Search `systemctl --user` for any services that may not be aligned in any way and align them.
Search all scripts for anything that may be unaligned and align it.
Streamline and simplify all automation. Improve. Extend. Make perfect. We already have some automation that uses browser CDP but it's barely working. It needs to be seriously reworked. bird cli is buggy and restricted and should be avoided. Direct Chrome CDP is the way to go.

Keep track of the progress in TODO-twitter.md and keep architecture in decent-cloud-twitter-automation.md

- Analyze TODO-twitter.md
- Analyze decent-cloud-twitter-automation.md

Identify the highest-impact items that:
1. Deliver REAL value OR remove a downside or an issue
2. Require EITHER actual design decisions OR code changes / improvements
3. Can be completed in a single session, e.g. single week from a multi-week effort

Pick AS MANY OF THEM AS YOU CAN and complete them with subagents - one subagent per item.

For each of them, first build a working PoC as per the mandatory workflow, as python or bash scripts, or unit/integration tests, but not as individual shell commands. Then implement with failing tests that you after that get to pass by implementing the missing functionality, as per TDD.

Then update TODO-twitter.md to a) remove all fully done items, b) update existing items with ANY NEW DETAILS, c) splitting into subitems, d) adding dependencies etc. that you may now have on them (IF VALUABLE for future implementation or activities).

If there are not enough MEANINGFUL items in TODO-twitter.md that you can do now, create a few subagents for the following: consider the app from the *point of view of building a follower base* - there is a TON of room for RADICALLY improving for follower count. Regardless of how radical these changes are - let's do them! Add them to TODO-twitter.md and we'll handle them in the follow-up session(s).

Update decent-cloud-twitter-automation.md if you made changes in how automation works, or the document is out of date.

When fully done:
- VERIFY (MANDATORY) that the actual services run as expected, BY RUNNING ALL OF THEM RIGHT NOW - we have seen many issues with scripts not running within services due to various permissions or env vars
- Commit all changes you made
