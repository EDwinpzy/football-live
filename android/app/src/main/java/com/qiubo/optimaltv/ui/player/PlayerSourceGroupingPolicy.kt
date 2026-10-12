package com.qiubo.optimaltv.ui.player

/** Keep original line indices: selecting a provider must not change switchLine's index. */
internal object PlayerSourceGroupingPolicy {
    data class Group(val provider: String, val lineIndices: List<Int>)

    fun groups(labels: List<String>): List<Group> {
        val groups = linkedMapOf<String, MutableList<Int>>()
        labels.forEachIndexed { index, label ->
            val separator = label.indexOfFirst { it == '·' || it == '•' || it == '・' }
            val provider = if (separator > 0) label.substring(0, separator).trim() else "其他信号源"
            groups.getOrPut(provider.ifBlank { "其他信号源" }) { mutableListOf() }.add(index)
        }
        return groups.map { (provider, indices) -> Group(provider, indices.toList()) }
    }
}
