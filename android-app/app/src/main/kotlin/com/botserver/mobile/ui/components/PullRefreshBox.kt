package com.botserver.mobile.ui.components

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.pulltorefresh.PullToRefreshContainer
import androidx.compose.material3.pulltorefresh.rememberPullToRefreshState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.nestedscroll.nestedScroll

/** A pull-to-refresh gesture wrapper around Material3's 1.2.x
 * (pre-`PullToRefreshBox`) API — this app is pinned to Compose BOM
 * 2024.06.00 / material3 1.2.1, whose pull-to-refresh surface is the
 * older `rememberPullToRefreshState()` + `PullToRefreshContainer` +
 * manual `nestedScroll` shape, not the newer all-in-one composable added
 * in material3 1.3.0. Named distinctly (not `PullToRefreshBox`) so a
 * future material3 upgrade to the real one doesn't collide with this.
 *
 * [refreshing] is the ViewModel's own loading flag — the gesture's
 * visual spinner state is driven by it rather than living independently,
 * so a refresh triggered from anywhere else (not just the pull gesture)
 * still shows/hides the indicator correctly. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PullRefreshBox(
    refreshing: Boolean,
    onRefresh: () -> Unit,
    modifier: Modifier = Modifier,
    content: @Composable BoxScope.() -> Unit,
) {
    val state = rememberPullToRefreshState()
    LaunchedEffect(state.isRefreshing) {
        if (state.isRefreshing) onRefresh()
    }
    LaunchedEffect(refreshing) {
        if (!refreshing) state.endRefresh()
    }
    Box(modifier.nestedScroll(state.nestedScrollConnection)) {
        content()
        PullToRefreshContainer(state = state, modifier = Modifier.align(Alignment.TopCenter))
    }
}
