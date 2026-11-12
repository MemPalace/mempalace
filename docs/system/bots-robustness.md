### Bots refuse early and never read a partial reply as final

- A streamed model reply only counts as final on a stop that means it: a paused turn, or tool calls closed by a non-tool stop, are bad responses. A confirm tool is refused before its button exists when the action would be refused anyway (free text that is not the person's words, a start the runner would turn down), and a free-text schema of any shape needs that check. A block shown after the person lost access reads `gone`.
