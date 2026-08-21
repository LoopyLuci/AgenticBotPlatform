package com.botserver.mobile.ui.components

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp

/** Mirrors bot/commands.py's HELP_TEXT — the same commands Telegram,
 * Discord, Slack, and the Support Bot's dispatch_command() all accept.
 * Also ported to the desktop app's composer (desktop-app/ui/main.js's
 * SLASH_COMMANDS/attachSlashMenu) — keep the three lists in sync. */
data class SlashCommand(val cmd: String, val args: String, val desc: String)

val SLASH_COMMANDS = listOf(
    SlashCommand("ask", "<text>", "Send a prompt (add --backend=api|cli|ui|hermes_cli|hermes_gateway to override)"),
    SlashCommand("status", "", "Health snapshot"),
    SlashCommand("backend", "show | set <action|default> <backend>", "Router config"),
    SlashCommand("model", "show | set <backend> <model>", "Per-backend model"),
    SlashCommand("mcp", "list | enable <name> | disable <name> | logs <name>", "MCP servers"),
    SlashCommand("start_desktop", "", "Launch Claude Desktop"),
    SlashCommand("stop_desktop", "", "Stop Claude Desktop"),
    SlashCommand("restart_desktop", "", "Restart Claude Desktop"),
    SlashCommand("project", "open <path>", "Set working dir for the next /ask"),
    SlashCommand("new_session", "", "Open a fresh linked chat in Claude Desktop/Hermes for this bot"),
    SlashCommand("help", "", "List available commands"),
)

/** Pops a Telegram-style "/" autocomplete list above a composer while the
 * user is typing a bare command name (a "/" with no space yet). Tapping a
 * row fills the input with "/cmd " — same behavior as the desktop app's
 * attachSlashMenu(). Caller owns the text field's state; this composable
 * only reads it and reports a selection back via onSelect. */
@Composable
fun SlashCommandSuggestions(text: String, onSelect: (String) -> Unit) {
    val visible = text.startsWith("/") && !text.contains(Regex("\\s"))
    val query = if (visible) text.removePrefix("/") else ""
    val filtered = if (visible) SLASH_COMMANDS.filter { it.cmd.startsWith(query, ignoreCase = true) } else emptyList()

    AnimatedVisibility(visible = filtered.isNotEmpty()) {
        Surface(
            shape = RoundedCornerShape(14.dp),
            tonalElevation = 3.dp,
            shadowElevation = 4.dp,
            modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp).heightIn(max = 240.dp),
        ) {
            LazyColumn {
                items(filtered, key = { it.cmd }) { c ->
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clickable { onSelect("/${c.cmd} ") }
                            .padding(horizontal = 14.dp, vertical = 10.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text(
                            "/${c.cmd}",
                            fontWeight = FontWeight.Bold,
                            color = MaterialTheme.colorScheme.primary,
                            style = MaterialTheme.typography.bodyMedium,
                        )
                        Spacer(Modifier.width(8.dp))
                        Text(
                            (if (c.args.isNotEmpty()) c.args + " — " else "") + c.desc,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f),
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                }
            }
        }
    }
}
