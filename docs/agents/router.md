# The model router: how it thinks, learns, and is taught

A bot on the **ABP Agent** backend with its model set to **`auto`** (or left blank) has its model
chosen by the router, `bot/model_router.py`. The router records every choice with its full
reasoning. It learns from what happens next and from your feedback. How it thinks is set by a
policy you can edit; each save is a new version you can restore. All of this is on the dashboard's
**Model Router** page, in both the web dashboard and the desktop app.

## How a choice is made

1. **What kind of task is it?** Each task gets one of six classes: `trivial`, `coding`,
   `hard_reasoning`, `long_context`, `vision` or `bulk`. The class comes from the task's words: the
   built-in patterns, plus any extra keywords the policy adds.
   - Images always make it `vision`, and a large amount of material makes it `long_context`.
   - Once there are enough **training examples**, a small classifier trained on them can override
     the keywords, when it is sure enough (by default 3 examples and 60% confidence).
2. **Who can do it?** A candidate is left out, with the reason recorded, when any of these is true:
   - a rule blocks it;
   - it is resting after failures;
   - it cannot call tools and the task needs them;
   - it cannot read images and the task has one;
   - its context window is too small;
   - its allowance is used up.
3. **How do the rest score?** Each candidate is scored on five parts, and the policy weights them per
   task class:

   | part | what it measures |
   |---|---|
   | quality | how good it is at this kind of task: a measured eval pass rate, or else a guess from the catalog; feedback moves either one |
   | economy | its price; free models score highest |
   | headroom | how much of its allowance is left right now |
   | reliability | how often its calls succeed, learned from every call ABP makes |
   | speed | its average response time, learned |

   After that:
   - **Rules** add to or subtract from the score. A **pin** puts the model first. **Prefer** and
     **avoid** add or subtract a boost you choose.
   - A **training example** that looks like this task, and prefers a particular model, gives that
     model up to 0.15 extra.
4. **Exploration.** A share of automatic picks (10% by default) is re-ranked by a random draw from
   what is known about each model. This gives a promising model that has had few chances a real
   try. Such a pick is marked *explored*, and the usual pick is recorded next to it.

The top pick answers. A bot on `auto` keeps that model for later turns, so a conversation stays on
one model. It chooses again when that model starts resting or is blocked, or on every turn if the
policy turns *sticky* off.

## What it learns, and from what

**From every call.** This includes calls from bots with a fixed model, not only bots on `auto`.

- **Reliability.** Successes and failures are counted, and older ones count for less: by default
  they count half after 14 days. A model it has never seen starts at 75%, below one that has proven
  itself.
- **Speed.** A moving average of how long each call takes.
- **Rests.** A failure is sorted into a kind first, because each kind means something different:

  | failure | what happens |
  |---|---|
  | 429 (rate limited) | rests 2 minutes, doubling each time it repeats, up to an hour |
  | 403 (reserved or not allowed) or 404 (withdrawn) | a short rest the first time; once it repeats (3 in a row by default), a week |
  | 401 (bad key) | the whole provider rests for a day |
  | 402 (out of credit) | rests for a day |
  | "does not support tools" | rests for a week |
  | server error or timeout | rests a minute |
  | context too long | not held against the model at all |

  A model that answers again after failures is logged as recovered.

**From your feedback on a decision.**

- **Good choice / Poor choice** moves the quality estimate for that model on that task class. If
  you change your vote, the old vote is replaced, not counted twice.
- **"It was really a…"** adds a training example for the classifier.
- **"It should have used…"** adds a model-preference example.

**From training examples you add by hand** (Model Router → Training), in the form "text → class",
"text → model", or both.

When a bot on `auto` finds its model has failed, it tries the next pick straight away (the policy's
*failover for auto*, on by default). It then keeps whichever model worked. Each failover is
recorded as a decision that follows the one that failed.

## Seeing all of it

The **Model Router** page has these tabs:

- **Overview.** Decision counts, success rate and how many picks explored. It also shows a timeline
  (answered, failed, pending), the mix of task classes, which models are resting (with a Release
  button), and a model-by-class outcome heatmap.
- **Decisions.** Every routed turn, with its mode:

  | mode | what it records |
  |---|---|
  | `auto` | the first pick for a bot on `auto` |
  | `sticky` | a turn that kept the earlier pick |
  | `reroute` | a turn that chose again |
  | `failover` | a pick made after the previous one failed |
  | `advise` | a question to `/route` or `suggest_model` |

  Open one to see **how it thought**:
  1. the class it chose and why, including what the classifier thought;
  2. who was left out and why;
  3. every candidate's score, as a bar split into the five parts, plus rules and examples;
  4. what happened: the outcome, error kind, time, calls, tokens, and the decisions before and after
     it.

  You can teach it right there.
- **Models.** What it has learned about each model:
  - its share of picks;
  - its reliability, with a band showing how sure it is;
  - its speed and feedback;
  - whether it is resting, and its last failure;
  - how it does per task class.

  Each model has buttons to **Rest**, **Release**, **Prefer**, **Block** and **Forget**.
- **Learning log.** Everything it learned or changed, and why: rests, recoveries, feedback,
  examples, policy saves and resets. Each entry links to the decision behind it.
- **Training.** Add and remove examples, and see how many examples each class has.
- **Policy.** The editor, with:
  - weights per class, with a live bar of the resulting shares;
  - extra keywords;
  - rules, with an optional end date;
  - every learning setting and rest length;
  - *sticky*, whether task text is kept, and how long decisions are kept.

  Save it with a note to make a new version, or edit it as JSON. The history shows each version's
  exact changes and lets you restore any of them.
- **Try it.** Routes a task the way a bot on `auto` would right now, and shows the full reasoning
  without running the task or recording it.

## Where it lives

- **Storage.** Everything is kept in `data/agent/router.db`: decisions, per-model statistics,
  rests, the learning log, training examples and policy versions. It is separate from `bot.db`, so
  the router can be reset on its own.
  - Decisions and log entries are kept for 30 days by default, and at most 20,000 of each.
  - Task text is kept only as its first 240 characters, with known secrets removed, and only while
    the policy's *record task text* is on.
- **API.** `/api/router/*`, listed at the top of `bot/dashboard/router_api.py`. Reading uses the
  dashboard's normal sign-in. Anything that changes what the router does needs the desktop dashboard
  token.
- **Settings.** The `native_agent.router` settings in `config/backends.yaml` still decide which
  models are candidates at all (`candidates`, `also`), whether routing is on (`enabled`), and the
  failover depth (`max_failover_hops`). Claude is never a candidate unless you list it there.
- **Failures in the router itself.** Recording and learning never raise into a turn. If
  `router.db` is locked or damaged, you lose the learning, never the reply.
