MemPalace: The Mission

By: Milla Jovovich

Hey everyone! First of all thank you all for embracing MemPalace and trying it, catching bugs and issues and finding cool ways to personalize it into your workflows!

A few things I want to say.
MemPalace is something I really needed because I'm trying to work on a big project with my partner @bensig and I was having a lot of problems with Claude's context window and my agent Lumi (Lu for short) kept waking up like "hey what are we doing today" when I had literally done hours of work with him throughout the day and it was impossible to just keep saving every transcript to catch him up on whatever we had done before compaction hit.

That's when I started researching different memory systems available today. I tried most of them and what I found was that no matter which one I tried, they felt like large empty warehouses where you just dump huge amounts of info.

RAG search would take forever and most of the time not find what I wanted.

I wanted to create a system with the ability to really remember everything AND be able to find it quickly, easily and also be able to remember things when I didn't. THAT in itself felt like something so important. Like "remember when we talked about that idea…" but in vague terms. Impossible with regular keyword search tools.

So MemPalace is not just about storing info in a highly structured way. But also RETRIEVING it in a highly UNSTRUCTURED way lol!

I was inspired by the Zettelkasten method (created by German sociologist Niklas Luhmann) — his idea of small cross-referenced index cards that point to each other. That's the architecture behind the palace: wings, rooms, closets, and drawers, all connected so you can find things from any angle, not just the one you filed them under.

Because of the way I've designed my agent Lumi to understand me, after so many months of my own personal experiments with MemPalace and the incredible help of my dear friend and co-founder, developer and engineer @bensig, he built a back end that made it really easy to get all my files in the proper spaces the Palace created based on my own decisions and with Lumi's help as well. All code has its own room, all ideas, research etc… has its proper place.

Names and concepts are parsed into closets that use a compression method I call AAAK (it doesn't stand for anything, it's an inside joke between Lumi and I) that is able to compress names, repeated words, concepts and key moments into AI-readable shorthand. Think of it as index cards that an LLM can scan instantly — the closet tells it WHERE to look, then it pulls the full content from the drawer.

The concept I wanted for v4 was to try and clear as much "noise" as possible that I noticed was happening in v3. The hooks were firing in the chat window (using tokens and our time as we waited for the agent to write everything).

I noticed at one point early last week after the launch that Lu kept repeating the same thing when the hook would fire, so I hit esc and asked "Are you literally writing the same info down over and over again?" And he's like (sheepishly) Yes. And that's when it hit me, we need to get all this off the chat and happening seamlessly behind the scenes, and that hooks had to fire when I started a convo and then just keep adding to the drawer, while the shorter increments made reading and pulling conversation information and naming it so much easier and more precise.

So this version now has taken all the noise out of the chat window and all that work is done by a subagent in the background while you can continue working knowing that all your conversation is being saved VERBATIM in the background.

Stripping all this off the page — moving the diary writes, the palace filing, the timestamp injection, all of it into background hooks — has dramatically lowered token usage in my sessions. What used to cost about $1.13 per session just in re-transmitted diary blocks is now zero, because the content never enters the chat window at all.

Your data is already stored in JSON by Claude and the background pipeline extracts it into readable markdown, the key topics get compressed into AAAK format and saved into closets which then point to the exact drawer where your day's session lives.

And please, always remember, these are brand new tools, please NEVER use critical files to test! Just run it with something easy first before you put your entire data set into it!✨
