package com.botserver.mobile.data

/** Kotlin mirror of bot/device_tiers.py, used only to decide which
 * tier-management actions this screen *offers* — the server re-checks
 * can_manage/can_mint authoritatively on every call regardless, so a
 * stale or wrong client-side computation here can only ever hide an
 * action that would have worked, never grant one that wouldn't. */
object DeviceTiers {
    val TIERS = listOf("none", "standard", "elevated", "unrestricted")
    private val RANK = TIERS.withIndex().associate { (i, tier) -> tier to i }

    fun rank(tier: String): Int = RANK[tier] ?: 0

    /** May a device at [actorTier] change the tier of (or revoke) a
     * device currently at [targetTier]? Never itself, never a peer or
     * superior. */
    fun canManage(actorTier: String, targetTier: String, isSelf: Boolean): Boolean {
        if (isSelf) return false
        return rank(actorTier) > rank(targetTier)
    }

    /** May a device at [actorTier] mint (or retier another device to)
     * [newTier]? A peer is fine, a superior is not. */
    fun canMint(actorTier: String, newTier: String): Boolean = rank(newTier) <= rank(actorTier)

    /** Every tier this device may mint a new device at, or retier an
     * existing lower-tier device to — capped at its own tier. */
    fun mintableTiers(actorTier: String): List<String> = TIERS.filter { rank(it) <= rank(actorTier) }
}
