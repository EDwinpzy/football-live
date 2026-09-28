package com.qiubo.optimaltv.data.catalog

import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import com.qiubo.optimaltv.data.model.CatalogMedia
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.InputStream
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.Path
import java.nio.channels.FileChannel
import java.nio.file.StandardOpenOption.READ
import java.nio.file.StandardCopyOption.ATOMIC_MOVE
import java.nio.file.StandardCopyOption.REPLACE_EXISTING
import java.security.DigestInputStream
import java.security.MessageDigest
import java.text.Normalizer
import java.util.Locale
import java.util.concurrent.atomic.AtomicBoolean
import java.util.zip.GZIPInputStream
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.HttpUrl.Companion.toHttpUrl
import org.json.JSONArray
import org.json.JSONObject

sealed interface CatalogRefreshResult {
    data class Installed(val version: String) : CatalogRefreshResult
    data class UpToDate(val version: String) : CatalogRefreshResult
    data class Failed(val reason: String) : CatalogRefreshResult
}

data class CatalogBrowseResult(val items: List<CatalogMedia>, val total: Int)
data class CatalogFilterOptions(val genres: List<String>, val regions: List<String>, val years: List<String>)

/**
 * Owns the replaceable, read-only metadata database. Room remains the source of truth for user data.
 * The constructor accepting a directory and seed supplier is internal so JVM tests can isolate files.
 */
class MetadataCatalogStore internal constructor(
    private val directory: File,
    private val client: OkHttpClient,
    apiOrigin: String,
    private val fullIntegrityCheck: (SQLiteDatabase) -> String = { db ->
        db.rawQuery("PRAGMA integrity_check", null).use { cursor ->
            if (!cursor.moveToFirst()) "" else cursor.getString(0)
        }
    },
    private val seedCatalogJson: () -> String,
    private val atomicMove: (Path, Path) -> Unit = { source, target -> Files.move(source, target, ATOMIC_MOVE, REPLACE_EXISTING) },
    private val syncParentDirectory: (Path) -> Unit = { parent ->
        if (File.separatorChar != '\\') FileChannel.open(parent, READ).use { it.force(true) }
    },
) {
    constructor(context: Context, client: OkHttpClient, apiOrigin: String) : this(
        directory = File(context.filesDir, DIRECTORY_NAME),
        client = client,
        apiOrigin = apiOrigin,
        seedCatalogJson = {
            kotlin.runCatching {
                context.assets.open(SEED_ASSET).bufferedReader(Charsets.UTF_8).use { it.readText() }
            }.getOrDefault("")
        },
    )

    private val origin = apiOrigin.toHttpUrl()
    private val refreshMutex = Mutex()
    private val currentFile: File get() = File(directory, DATABASE_NAME)
    private val fullRecoveryRequired = AtomicBoolean(false)

    /** Test-visible path also used by support diagnostics; callers must not write this file. */
    internal val currentDatabaseFile: File get() = currentFile

    suspend fun refreshIfNeeded(force: Boolean = false): CatalogRefreshResult = withContext(Dispatchers.IO) {
        refreshMutex.withLock {
            try {
                refreshLocked(force)
            } catch (cancelled: kotlinx.coroutines.CancellationException) {
                throw cancelled
            } catch (error: Exception) {
                CatalogRefreshResult.Failed(error.message ?: error.javaClass.simpleName)
            }
        }
    }

    suspend fun hasInstalledDatabase(): Boolean = withContext(Dispatchers.IO) {
        if (!currentFile.isFile) return@withContext false
        runCatching { readInstalledVersion(currentFile) }
            .onFailure { fullRecoveryRequired.set(true) }
            .isSuccess
    }

    /** Filter and page inside SQLite so browse requests never materialize the full catalogue. */
    suspend fun browsePage(
        mediaType: String = "",
        genre: String = "",
        region: String = "",
        year: String = "",
        minimumRating: Double? = null,
        sort: String = "hot",
        page: Int = 1,
        pageSize: Int = 48,
    ): CatalogBrowseResult = withContext(Dispatchers.IO) {
        val key = mediaType.trim().lowercase(Locale.ROOT)
        val categoryTypes = when (key) {
            "movie" -> listOf("movie", "film")
            "tv" -> listOf("tv", "series", "tv_series", "show")
            "anime" -> listOf("anime", "animation")
            "variety" -> listOf("variety")
            "short" -> listOf("short")
            else -> emptyList()
        }
        val where = ArrayList<String>()
        val args = ArrayList<String>()
        if (categoryTypes.isNotEmpty()) {
            where += "lower(m.media_type) IN (${categoryTypes.joinToString(",") { "?" }})"
            args += categoryTypes
        }
        if (genre.isNotBlank()) {
            where += "EXISTS (SELECT 1 FROM media_genre mg JOIN genre g ON g.id = mg.genre_id WHERE mg.media_id = m.id AND g.name = ?)"
            args += genre
        }
        if (region.isNotBlank()) {
            where += "EXISTS (SELECT 1 FROM media_region mr JOIN region r ON r.id = mr.region_id WHERE mr.media_id = m.id AND r.name LIKE ? ESCAPE '\\')"
            args += "%${region.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")}%"
        }
        if (year.isNotBlank()) {
            where += "CAST(m.year AS TEXT) = ?"
            args += year
        }
        if (minimumRating != null) {
            // Cast back to REAL: a TEXT-bound threshold compares greater than every
            // numeric score in SQLite, so the >= predicate would never match.
            where += "CAST(COALESCE((SELECT score FROM rating WHERE media_id = m.id ORDER BY CASE WHEN provider = 'douban' THEN 0 ELSE 1 END LIMIT 1), 0) AS REAL) >= CAST(? AS REAL)"
            args += minimumRating.toString()
        }
        val whereSql = where.takeIf(List<String>::isNotEmpty)?.joinToString(" AND ", prefix = " WHERE ").orEmpty()
        val countArgs = args.toTypedArray()
        val safePageSize = pageSize.coerceIn(1, 100)
        val offset = (page.coerceAtLeast(1) - 1).toLong() * safePageSize
        val orderSql = when (sort.lowercase(Locale.ROOT)) {
            "new", "2" -> "m.year DESC, m.id DESC"
            "rating", "1" -> "COALESCE((SELECT score FROM rating WHERE media_id = m.id ORDER BY CASE WHEN provider = 'douban' THEN 0 ELSE 1 END LIMIT 1), 0) DESC, m.id DESC"
            else -> "m.popularity DESC, m.id DESC"
        }
        val result: CatalogBrowseResult? = runCatching {
            if (!currentFile.isFile) null else openReadOnly(currentFile).use { db ->
                val total = db.rawQuery("SELECT COUNT(*) FROM media m$whereSql", countArgs).use { cursor ->
                    if (cursor.moveToFirst()) cursor.getInt(0) else 0
                }
                val queryArgs = countArgs + arrayOf(safePageSize.toString(), offset.toString())
                val ids = db.rawQuery(
                    "SELECT m.id FROM media m$whereSql ORDER BY $orderSql LIMIT CAST(? AS INTEGER) OFFSET CAST(? AS INTEGER)",
                    queryArgs,
                ).use { cursor -> buildList { while (cursor.moveToNext()) add(cursor.getLong(0)) } }
                CatalogBrowseResult(if (ids.isEmpty()) emptyList() else readMedia(db, ids), total)
            }
        }.onFailure { fullRecoveryRequired.set(true) }.getOrNull()
        result ?: run {
            val seed = readSeedCatalog().asSequence()
                .filter { categoryTypes.isEmpty() || it.mediaType.trim().lowercase(Locale.ROOT) in categoryTypes }
                .filter { genre.isBlank() || genre in it.genres }
                .filter { region.isBlank() || it.regions.any { value -> value.contains(region, ignoreCase = true) } }
                .filter { year.isBlank() || year == it.year }
                .filter { minimumRating == null || it.rating >= minimumRating }
                .toList()
            val sorted = when (sort.lowercase(Locale.ROOT)) {
                "new", "2" -> seed.sortedWith(compareByDescending<CatalogMedia> { it.year.toIntOrNull() ?: 0 }.thenByDescending { it.mytvId })
                "rating", "1" -> seed.sortedWith(compareByDescending<CatalogMedia> { it.rating }.thenByDescending { it.mytvId })
                else -> seed.sortedWith(compareByDescending<CatalogMedia> { it.popularity }.thenByDescending { it.mytvId })
            }
            val start = offset.coerceAtMost(Int.MAX_VALUE.toLong()).toInt()
            CatalogBrowseResult(sorted.drop(start).take(safePageSize), sorted.size)
        }
    }

    suspend fun filterOptions(mediaType: String): CatalogFilterOptions = withContext(Dispatchers.IO) {
        val categoryTypes = categoryTypes(mediaType)
        runCatching {
            if (!currentFile.isFile) null else openReadOnly(currentFile).use { db ->
                val where = if (categoryTypes.isEmpty()) "" else
                    " WHERE lower(m.media_type) IN (${categoryTypes.joinToString(",") { "?" }})"
                val args = categoryTypes.toTypedArray()
                fun values(sql: String): List<String> = db.rawQuery(sql, args).use { cursor ->
                    buildList { while (cursor.moveToNext()) cursor.getString(0)?.takeIf(String::isNotBlank)?.let(::add) }
                }
                val yearWhere = if (where.isBlank()) " WHERE m.year IS NOT NULL" else "$where AND m.year IS NOT NULL"
                CatalogFilterOptions(
                    genres = values("SELECT DISTINCT g.name FROM genre g JOIN media_genre mg ON mg.genre_id = g.id JOIN media m ON m.id = mg.media_id$where ORDER BY g.name"),
                    regions = values("SELECT DISTINCT r.name FROM region r JOIN media_region mr ON mr.region_id = r.id JOIN media m ON m.id = mr.media_id$where ORDER BY r.name"),
                    years = values("SELECT DISTINCT CAST(m.year AS TEXT) FROM media m$yearWhere ORDER BY m.year DESC"),
                )
            }
        }.onFailure { fullRecoveryRequired.set(true) }.getOrNull() ?: run {
            val wanted = categoryTypes.map(String::lowercase).toSet()
            val seed = readSeedCatalog().filter { wanted.isEmpty() || it.mediaType.lowercase() in wanted }
            CatalogFilterOptions(
                genres = seed.flatMap(CatalogMedia::genres).distinct().sorted(),
                regions = seed.flatMap(CatalogMedia::regions).distinct().sorted(),
                years = seed.map(CatalogMedia::year).filter(String::isNotBlank).distinct().sortedDescending(),
            )
        }
    }

    suspend fun categoryKeys(): List<String> = withContext(Dispatchers.IO) {
        val result = runCatching {
            if (!currentFile.isFile) null else openReadOnly(currentFile).use { db ->
                db.rawQuery(
                    """
                    SELECT CASE
                        WHEN lower(media_type) IN ('tv', 'series', 'tv_series', 'show') THEN 'tv'
                        WHEN lower(media_type) IN ('anime', 'animation') THEN 'anime'
                        WHEN lower(media_type) IN ('variety', 'short') THEN lower(media_type)
                        ELSE 'movie'
                    END AS category
                    FROM media
                    GROUP BY category
                    ORDER BY MAX(popularity) DESC
                    """.trimIndent(),
                    null,
                ).use { cursor -> buildList<String> { while (cursor.moveToNext()) add(cursor.getString(0)) } }
            }
        }.onFailure { fullRecoveryRequired.set(true) }.getOrNull()
        result ?: readSeedCatalog().map { categoryKeyForMediaType(it.mediaType) }.distinct()
    }

    suspend fun topRows(mediaType: String, sort: String = "hot", limit: Int = 24): List<CatalogMedia> = withContext(Dispatchers.IO) {
        val types = categoryTypes(mediaType)
        runCatching {
            if (!currentFile.isFile) null else openReadOnly(currentFile).use { db ->
                val where = if (types.isEmpty()) "" else " WHERE lower(m.media_type) IN (${types.joinToString(",") { "?" }})"
                val order = when (sort.lowercase(Locale.ROOT)) {
                    "new" -> "m.year DESC, m.id DESC"
                    "updated" -> "m.updated_at DESC, m.id DESC"
                    "rating" -> "COALESCE((SELECT score FROM rating WHERE media_id = m.id ORDER BY CASE WHEN provider = 'douban' THEN 0 ELSE 1 END LIMIT 1), 0) DESC, m.id DESC"
                    else -> "m.popularity DESC, m.id DESC"
                }
                val ids = db.rawQuery(
                    "SELECT m.id FROM media m$where ORDER BY $order LIMIT ?",
                    types.toTypedArray() + arrayOf(limit.coerceIn(1, 100).toString()),
                ).use { cursor -> buildList { while (cursor.moveToNext()) add(cursor.getLong(0)) } }
                if (ids.isEmpty()) emptyList() else readMedia(db, ids)
            }
        }.onFailure { fullRecoveryRequired.set(true) }.getOrNull() ?: readSeedCatalog()
            .filter { types.isEmpty() || it.mediaType.lowercase() in types }
            .sortedWith(when (sort.lowercase(Locale.ROOT)) {
                "new" -> compareByDescending<CatalogMedia> { it.year.toIntOrNull() ?: 0 }
                "updated" -> compareByDescending(CatalogMedia::updatedAt)
                "rating" -> compareByDescending(CatalogMedia::rating)
                else -> compareByDescending(CatalogMedia::popularity)
            })
            .take(limit.coerceIn(1, 100))
    }

    /**
     * Rows materialised into the in-memory catalogue. [CatalogSnapshotPolicy] bounds the on-disk
     * snapshot, but the in-memory catalogue must stay full: `itemById`, `siblings` and `byCategory`
     * read from memory only, so a bounded preview makes everything past the cutoff unresolvable
     * (player's `itemById(...)?.categoryId`, search category jumps, first-screen hero preload).
     * `readMedia` is a single indexed query and the baseline loaded the whole catalogue here too.
     */
    suspend fun previewRows(limit: Int = MAX_IN_MEMORY_ROWS): List<CatalogMedia> = withContext(Dispatchers.IO) {
        val cap = limit.coerceIn(1, MAX_IN_MEMORY_ROWS)
        runCatching {
            if (!currentFile.isFile) null else openReadOnly(currentFile).use { db -> readMedia(db, limit = cap) }
        }.onFailure { fullRecoveryRequired.set(true) }.getOrNull() ?: readSeedCatalog().take(cap)
    }

    private fun categoryTypes(mediaType: String): List<String> = when (mediaType.trim().lowercase(Locale.ROOT)) {
        "movie" -> listOf("movie", "film")
        "tv" -> listOf("tv", "series", "tv_series", "show")
        "anime" -> listOf("anime", "animation")
        "variety" -> listOf("variety")
        "short" -> listOf("short")
        else -> emptyList()
    }

    private fun categoryKeyForMediaType(mediaType: String): String = when (mediaType.trim().lowercase(Locale.ROOT)) {
        "tv", "series", "tv_series", "show" -> "tv"
        "anime", "animation" -> "anime"
        "variety", "short" -> mediaType.trim().lowercase(Locale.ROOT)
        else -> "movie"
    }

    suspend fun findByMyTvId(mytvId: String): CatalogMedia? = withContext(Dispatchers.IO) {
        val id = mytvId.trim().toLongOrNull()?.takeIf { it > 0 } ?: return@withContext null
        if (!currentFile.isFile) return@withContext readSeedCatalog().firstOrNull { it.mytvId == id.toString() }
        runCatching { openReadOnly(currentFile).use { db -> readMedia(db, ids = listOf(id)).firstOrNull() } }
            .onFailure { fullRecoveryRequired.set(true) }
            .getOrElse { readSeedCatalog().firstOrNull { it.mytvId == id.toString() } }
    }

    suspend fun findByLegacyDoubanId(doubanId: String): CatalogMedia? = withContext(Dispatchers.IO) {
        val wanted = doubanId.trim().removePrefix("douban:").takeIf(String::isNotBlank)
            ?: return@withContext null
        if (!currentFile.isFile) return@withContext readSeedCatalog().firstOrNull { it.externalIds["douban"] == wanted }
        runCatching {
            openReadOnly(currentFile).use { db ->
                db.rawQuery(
                    "SELECT media_id FROM external_id WHERE provider = 'douban' AND external_id = ? LIMIT 1",
                    arrayOf(wanted),
                ).use { cursor ->
                    if (cursor.moveToFirst()) readMedia(db, ids = listOf(cursor.getLong(0))).firstOrNull() else null
                }
            }
        }.onFailure { fullRecoveryRequired.set(true) }
            .getOrElse { readSeedCatalog().firstOrNull { it.externalIds["douban"] == wanted } }
    }

    suspend fun search(query: String, limit: Int = DEFAULT_SEARCH_LIMIT): List<CatalogMedia> = withContext(Dispatchers.IO) {
        val normalized = normalizeQuery(query)
        if (normalized.isBlank()) return@withContext emptyList()
        val safeLimit = limit.coerceIn(1, MAX_SEARCH_LIMIT)
        val local = runCatching {
            if (!currentFile.isFile) emptyList()
            else openReadOnly(currentFile).use { db -> searchDatabase(db, query, normalized, safeLimit) }
        }.onFailure { fullRecoveryRequired.set(true) }.getOrDefault(emptyList())
        if (currentFile.isFile && !fullRecoveryRequired.get()) local
        else readSeedCatalog().asSequence()
            .filter { media -> media.searchTerms.any { it.contains(normalized, ignoreCase = true) } }
            .take(safeLimit)
            .toList()
    }

    private fun refreshLocked(force: Boolean): CatalogRefreshResult {
        directory.mkdirs()
        val manifest = fetchManifest()
        val verifyCurrentIntegrity = force || fullRecoveryRequired.get()
        val currentVersion = if (currentFile.isFile) runCatching {
            if (verifyCurrentIntegrity) validateDatabase(currentFile) else readInstalledVersion(currentFile)
        }.onFailure { fullRecoveryRequired.set(true) }.getOrNull() else null
        if (currentVersion == manifest.latestVersion) {
            fullRecoveryRequired.set(false)
            return CatalogRefreshResult.UpToDate(currentVersion)
        }

        if (currentVersion != null) {
            val delta = manifest.deltas.firstOrNull { it.fromVersion == currentVersion && it.toVersion == manifest.latestVersion }
            if (delta != null) {
                try {
                    if (installDelta(delta, currentVersion, manifest.latestVersion)) {
                        fullRecoveryRequired.set(false)
                        return CatalogRefreshResult.Installed(manifest.latestVersion)
                    }
                } catch (cancelled: kotlinx.coroutines.CancellationException) {
                    throw cancelled
                } catch (_: Exception) {
                    // A damaged/incompatible delta is discarded; the verified full package is the fallback.
                }
            }
        }

        installFull(manifest.full, manifest.latestVersion)
        return CatalogRefreshResult.Installed(manifest.latestVersion)
    }

    private fun fetchManifest(): CatalogManifest {
        val url = origin.resolve(MANIFEST_PATH) ?: error("invalid manifest URL")
        val request = Request.Builder().url(url).header("User-Agent", "MyTV Android").get().build()
        val json = client.newCall(request).execute().use { response ->
            require(response.isSuccessful) { "manifest HTTP ${response.code}" }
            JSONObject(response.body?.string().orEmpty())
        }
        val schema = json.optInt("schema", -1)
        require(schema == SUPPORTED_SCHEMA) { "unsupported manifest schema: $schema" }
        val latest = json.optString("latest_version")
        require(VERSION_PATTERN.matches(latest)) { "unsupported catalog data version" }
        val fullObject = json.optJSONObject("full") ?: error("manifest full package missing")
        val full = fullObject.toPackageEntry(requireUncompressed = true)
        require(full.version == latest) { "manifest full version does not match latest_version" }
        val deltas = json.optJSONArray("deltas").toJsonList()
            .mapNotNull { item -> runCatching { item.toDeltaEntry() }.getOrNull() }
        return CatalogManifest(latest, full, deltas)
    }

    private fun JSONObject.toPackageEntry(requireUncompressed: Boolean): PackageEntry {
        val version = optString("version")
        require(VERSION_PATTERN.matches(version)) { "invalid package version" }
        val size = optLong("size", -1)
        require(size in 1..MAX_COMPRESSED_BYTES) { "invalid package size" }
        val digest = optString("sha256").lowercase(Locale.ROOT)
        require(SHA256_PATTERN.matches(digest)) { "invalid package SHA-256" }
        require(optString("compression") == "gzip") { "unsupported package compression" }
        val uncompressedSize = if (requireUncompressed) optLong("uncompressed_size", -1) else -1L
        val uncompressedSha = if (requireUncompressed) optString("uncompressed_sha256").lowercase(Locale.ROOT) else ""
        if (requireUncompressed) {
            require(uncompressedSize in 1..MAX_DATABASE_BYTES) { "invalid uncompressed package size" }
            require(SHA256_PATTERN.matches(uncompressedSha)) { "invalid uncompressed SHA-256" }
        }
        return PackageEntry(
            version = version,
            url = optString("url").takeIf(String::isNotBlank) ?: error("package URL missing"),
            sha256 = digest,
            size = size,
            uncompressedSha256 = uncompressedSha,
            uncompressedSize = uncompressedSize,
        )
    }

    private fun JSONObject.toDeltaEntry(): DeltaEntry {
        val from = optString("from")
        val to = optString("to")
        require(VERSION_PATTERN.matches(from) && VERSION_PATTERN.matches(to)) { "invalid delta version range" }
        val size = optLong("size", -1)
        require(size in 1..MAX_COMPRESSED_BYTES) { "invalid delta size" }
        val digest = optString("sha256").lowercase(Locale.ROOT)
        require(SHA256_PATTERN.matches(digest)) { "invalid delta SHA-256" }
        require(optString("compression") == "gzip") { "unsupported delta compression" }
        val url = optString("url").takeIf(String::isNotBlank) ?: error("delta URL missing")
        return DeltaEntry(from, to, url, size, digest)
    }

    private fun installFull(entry: PackageEntry, expectedVersion: String) {
        val compressed = File(directory, STAGING_GZIP)
        val staged = File(directory, STAGING_DATABASE)
        try {
            downloadVerified(entry.url, compressed, entry.size, entry.sha256)
            gunzipVerified(compressed, staged, entry.uncompressedSize, entry.uncompressedSha256, MAX_DATABASE_BYTES)
            require(validateDatabase(staged, expectedVersion) == expectedVersion) { "full package data version mismatch" }
            atomicReplace(staged)
            fullRecoveryRequired.set(false)
        } finally {
            compressed.delete()
            staged.delete()
        }
    }

    private fun installDelta(entry: DeltaEntry, currentVersion: String, expectedVersion: String): Boolean {
        if (entry.fromVersion != currentVersion || entry.toVersion != expectedVersion) return false
        val compressed = File(directory, STAGING_DELTA_GZIP)
        val sqlFile = File(directory, STAGING_DELTA_SQL)
        val staged = File(directory, STAGING_DATABASE)
        try {
            downloadVerified(entry.url, compressed, entry.size, entry.sha256)
            gunzipVerified(compressed, sqlFile, expectedSize = null, expectedSha256 = null, MAX_DELTA_BYTES)
            copyAndSync(currentFile, staged)
            applyDelta(staged, sqlFile.readText(Charsets.UTF_8))
            require(validateDatabase(staged, expectedVersion) == expectedVersion) { "delta target version mismatch" }
            atomicReplace(staged)
            fullRecoveryRequired.set(false)
            return true
        } finally {
            compressed.delete()
            sqlFile.delete()
            staged.delete()
        }
    }

    private fun downloadVerified(urlText: String, target: File, expectedSize: Long, expectedSha256: String) {
        val url = resolveArtifactUrl(urlText)
        val request = Request.Builder().url(url).header("User-Agent", "MyTV Android").get().build()
        val digest = MessageDigest.getInstance("SHA-256")
        var total = 0L
        try {
            client.newCall(request).execute().use { response ->
                require(response.isSuccessful) { "package HTTP ${response.code}" }
                val body = response.body ?: error("empty package response")
                if (body.contentLength() >= 0) require(body.contentLength() == expectedSize) { "package size header mismatch" }
                target.outputStream().use { output ->
                    val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                    DigestInputStream(body.byteStream(), digest).use { input ->
                        while (true) {
                            val count = input.read(buffer)
                            if (count < 0) break
                            total += count
                            require(total <= expectedSize && total <= MAX_COMPRESSED_BYTES) { "package exceeded manifest size" }
                            output.write(buffer, 0, count)
                        }
                    }
                    output.flush()
                    (output as? FileOutputStream)?.fd?.sync()
                }
            }
            require(total == expectedSize) { "package size mismatch" }
            require(digest.digest().toHex() == expectedSha256) { "package SHA-256 mismatch" }
        } catch (error: Throwable) {
            target.delete()
            throw error
        }
    }

    private fun gunzipVerified(
        source: File,
        target: File,
        expectedSize: Long?,
        expectedSha256: String?,
        maxBytes: Long,
    ) {
        val digest = MessageDigest.getInstance("SHA-256")
        var total = 0L
        try {
            FileInputStream(source).use { raw ->
                GZIPInputStream(raw).use { gzip ->
                    target.outputStream().use { output ->
                        val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                        while (true) {
                            val count = gzip.read(buffer)
                            if (count < 0) break
                            total += count
                            require(total <= maxBytes) { "uncompressed package exceeded size limit" }
                            digest.update(buffer, 0, count)
                            output.write(buffer, 0, count)
                        }
                        output.flush()
                        (output as? FileOutputStream)?.fd?.sync()
                    }
                }
            }
            if (expectedSize != null) require(total == expectedSize) { "uncompressed package size mismatch" }
            if (expectedSha256 != null) require(digest.digest().toHex() == expectedSha256) { "uncompressed package SHA-256 mismatch" }
        } catch (error: Throwable) {
            target.delete()
            throw error
        }
    }

    private fun readInstalledVersion(file: File): String {
        require(file.isFile && file.length() > 0) { "catalog database missing" }
        val db = SQLiteDatabase.openDatabase(file.absolutePath, null, SQLiteDatabase.OPEN_READONLY)
        try {
            val schema = scalar(db, "SELECT value FROM meta WHERE key = 'schema_version'")
            require(schema == SUPPORTED_SCHEMA.toString()) { "unsupported catalog schema: $schema" }
            val dataVersion = scalar(db, "SELECT value FROM meta WHERE key = 'data_version'")
            require(VERSION_PATTERN.matches(dataVersion)) { "invalid catalog data version" }
            return dataVersion
        } finally {
            db.close()
        }
    }

    private fun validateDatabase(
        file: File,
        expectedVersion: String? = null,
        checkIntegrity: Boolean = true,
    ): String {
        require(file.isFile && file.length() > 0) { "catalog database missing" }
        val db = SQLiteDatabase.openDatabase(file.absolutePath, null, SQLiteDatabase.OPEN_READONLY)
        try {
            if (checkIntegrity) {
                val integrity = fullIntegrityCheck(db)
                require(integrity.equals("ok", ignoreCase = true)) { "catalog integrity_check failed" }
            }
            val schema = scalar(db, "SELECT value FROM meta WHERE key = 'schema_version'")
            require(schema == SUPPORTED_SCHEMA.toString()) { "unsupported catalog schema: $schema" }
            REQUIRED_COLUMNS.forEach { (table, columns) ->
                val actual = mutableSetOf<String>()
                db.rawQuery("PRAGMA table_info('$table')", null).use { cursor ->
                    while (cursor.moveToNext()) actual += cursor.getString(cursor.getColumnIndexOrThrow("name"))
                }
                require(actual.containsAll(columns)) { "catalog table $table has unsupported columns" }
            }
            val dataVersion = scalar(db, "SELECT value FROM meta WHERE key = 'data_version'")
            require(VERSION_PATTERN.matches(dataVersion)) { "invalid catalog data version" }
            if (expectedVersion != null) require(dataVersion == expectedVersion) { "catalog data version mismatch" }
            return dataVersion
        } finally {
            db.close()
        }
    }

    private fun applyDelta(file: File, script: String) {
        val statements = splitSqlStatements(script)
        require(statements.isNotEmpty()) { "empty delta SQL" }
        var began = false
        var committed = false
        val db = SQLiteDatabase.openDatabase(file.absolutePath, null, SQLiteDatabase.OPEN_READWRITE)
        try {
            db.setForeignKeyConstraintsEnabled(false)
            db.beginTransaction()
            try {
                for (statement in statements) {
                    val trimmed = statement.trim()
                    val upper = trimmed.uppercase(Locale.ROOT)
                    when {
                        upper.startsWith("PRAGMA FOREIGN_KEYS") -> Unit
                        upper.startsWith("BEGIN") -> {
                            require(!began && !committed) { "malformed delta transaction" }
                            began = true
                        }
                        upper == "COMMIT" || upper == "COMMIT TRANSACTION" -> {
                            require(began && !committed) { "malformed delta transaction" }
                            committed = true
                        }
                        upper.startsWith("ROLLBACK") -> error("delta must not roll back its transaction")
                        else -> {
                            require(began && !committed) { "delta SQL is outside its transaction" }
                            db.execSQL(trimmed)
                        }
                    }
                }
                require(began && committed) { "malformed delta transaction" }
                db.setTransactionSuccessful()
            } finally {
                db.endTransaction()
                db.setForeignKeyConstraintsEnabled(true)
            }
        } finally {
            db.close()
        }
    }

    private fun splitSqlStatements(script: String): List<String> {
        val result = mutableListOf<String>()
        val current = StringBuilder()
        var quote: Char? = null
        var lineComment = false
        var blockComment = false
        var index = 0
        while (index < script.length) {
            val char = script[index]
            val next = script.getOrNull(index + 1)
            when {
                lineComment -> {
                    if (char == '\n') lineComment = false
                }
                blockComment -> {
                    if (char == '*' && next == '/') {
                        blockComment = false
                        index++
                    }
                }
                quote != null -> {
                    current.append(char)
                    if (char == quote) {
                        if (next == quote) {
                            current.append(next)
                            index++
                        } else quote = null
                    }
                }
                char == '-' && next == '-' -> {
                    lineComment = true
                    index++
                }
                char == '/' && next == '*' -> {
                    blockComment = true
                    index++
                }
                char == '\'' || char == '"' || char == '`' -> {
                    quote = char
                    current.append(char)
                }
                char == ';' -> {
                    current.toString().trim().takeIf(String::isNotEmpty)?.let(result::add)
                    current.setLength(0)
                }
                else -> current.append(char)
            }
            index++
        }
        require(quote == null && !blockComment) { "malformed delta SQL" }
        current.toString().trim().takeIf(String::isNotEmpty)?.let(result::add)
        return result
    }

    private fun searchDatabase(db: SQLiteDatabase, rawQuery: String, query: String, limit: Int): List<CatalogMedia> {
        val ftsIds = runCatching {
            val tokens = rawQuery.trim().split(Regex("\\s+")).mapNotNull { part ->
                val normalized = normalizeQuery(part).replace("\"", "")
                normalized.takeIf(String::isNotBlank)?.let { "\"$it\"*" }
            }
            if (tokens.isEmpty()) emptyList()
            else db.rawQuery(
                "SELECT media_id FROM search_fts WHERE search_fts MATCH ? ORDER BY rank LIMIT ?",
                arrayOf(tokens.joinToString(" AND "), limit.toString()),
            ).use { cursor -> buildList { while (cursor.moveToNext()) cursor.getString(0).toLongOrNull()?.let(::add) } }
        }.getOrDefault(emptyList())
        val textIds = if (ftsIds.isNotEmpty()) ftsIds else searchLikeIds(db, query, limit)
        val exactYearIds = searchYearIds(db, query, limit)
        val regionIds = searchRegionIds(db, query, limit)
        val candidateIds = (textIds + exactYearIds + regionIds).distinct().take(limit)
        if (candidateIds.isEmpty()) return emptyList()
        val placeholders = candidateIds.joinToString(",") { "?" }
        val candidateOrder = candidateIds.mapIndexed { index, id -> "WHEN $id THEN $index" }.joinToString(" ")
        val escapedQuery = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        val orderedIds = db.rawQuery(
            """
            SELECT media_id FROM search_doc
            WHERE media_id IN ($placeholders)
            ORDER BY CASE WHEN title_norm = ? THEN 0
                          WHEN title_norm LIKE ? ESCAPE '\' THEN 1
                          ELSE 2 END,
                     CASE media_id $candidateOrder ELSE ${candidateIds.size} END
            LIMIT ?
            """.trimIndent(),
            candidateIds.map(Long::toString).toTypedArray() + arrayOf(query, "${escapedQuery}%", limit.toString()),
        ).use { cursor -> buildList { while (cursor.moveToNext()) add(cursor.getLong(0)) } }
        return if (orderedIds.isEmpty()) emptyList() else readMedia(db, ids = orderedIds)
    }

    private fun searchYearIds(db: SQLiteDatabase, query: String, limit: Int): List<Long> {
        if (!query.matches(Regex("\\d{4}"))) return emptyList()
        return db.rawQuery(
            "SELECT id FROM media WHERE CAST(year AS TEXT) = ? ORDER BY popularity DESC, id DESC LIMIT ?",
            arrayOf(query, limit.toString()),
        ).use { cursor -> buildList { while (cursor.moveToNext()) add(cursor.getLong(0)) } }
    }

    private fun searchRegionIds(db: SQLiteDatabase, query: String, limit: Int): List<Long> {
        val escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return db.rawQuery(
            """
            SELECT DISTINCT mr.media_id
            FROM media_region mr
            JOIN region r ON r.id = mr.region_id
            JOIN media m ON m.id = mr.media_id
            WHERE lower(r.name) LIKE ? ESCAPE '\'
            ORDER BY m.popularity DESC, mr.media_id DESC
            LIMIT ?
            """.trimIndent(),
            arrayOf("%$escaped%", limit.toString()),
        ).use { cursor -> buildList { while (cursor.moveToNext()) add(cursor.getLong(0)) } }
    }

    private fun searchLikeIds(db: SQLiteDatabase, query: String, limit: Int): List<Long> {
        val escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        val contains = "%$escaped%"
        val prefix = "$escaped%"
        val fields = listOf("title_norm", "aliases_norm", "pinyin", "pinyin_alts", "initials", "initials_alts", "people_norm", "people_pinyin", "genres_norm")
        val where = fields.joinToString(" OR ") { "$it LIKE ? ESCAPE '\\'" }
        val order = """
            CASE WHEN title_norm = ? THEN 0
                 WHEN title_norm LIKE ? ESCAPE '\' THEN 1
                 WHEN aliases_norm LIKE ? ESCAPE '\' OR pinyin LIKE ? ESCAPE '\' OR initials LIKE ? ESCAPE '\' THEN 2
                 ELSE 3 END,
            popularity DESC, media_id DESC
        """.trimIndent()
        val finalArgs = ArrayList<String>(fields.size + 6).apply {
            addAll(Array(fields.size) { contains })
            add(query)
            add(prefix)
            add(prefix)
            add(prefix)
            add(prefix)
            add(limit.toString())
        }
        return db.rawQuery("SELECT media_id FROM search_doc WHERE $where ORDER BY $order LIMIT ?", finalArgs.toTypedArray())
            .use { cursor -> buildList { while (cursor.moveToNext()) add(cursor.getLong(0)) } }
    }

    private fun readMedia(db: SQLiteDatabase, ids: List<Long>? = null, limit: Int = Int.MAX_VALUE): List<CatalogMedia> {
        val selection = if (ids == null) "" else " WHERE m.id IN (${ids.joinToString(",") { "?" }})"
        val sortAndLimit = if (ids == null) " ORDER BY m.popularity DESC, m.id DESC LIMIT $limit" else
            " ORDER BY CASE m.id ${ids.mapIndexed { index, id -> "WHEN $id THEN $index" }.joinToString(" ")} ELSE ${ids.size} END"
        val args = ids?.map(Long::toString)?.toTypedArray()
        val sql = """
            SELECT m.id, m.media_type, m.title, m.original_title, m.year, m.overview, m.popularity, m.updated_at,
                   (SELECT original_url FROM image WHERE media_id = m.id AND image_type = 'poster' LIMIT 1) AS poster_url,
                   (SELECT score FROM rating WHERE media_id = m.id ORDER BY CASE WHEN provider = 'douban' THEN 0 ELSE 1 END LIMIT 1) AS rating,
                   (SELECT group_concat(provider || char(30) || external_id, char(31)) FROM external_id WHERE media_id = m.id) AS external_ids,
                   (SELECT group_concat(g.name, char(31)) FROM media_genre mg JOIN genre g ON g.id = mg.genre_id WHERE mg.media_id = m.id) AS genres,
                   (SELECT group_concat(r.name, char(31)) FROM media_region mr JOIN region r ON r.id = mr.region_id WHERE mr.media_id = m.id) AS regions,
                   (SELECT group_concat(p.name, char(31)) FROM credit c JOIN person p ON p.id = c.person_id WHERE c.media_id = m.id AND lower(c.department) IN ('acting', 'actor') ORDER BY c.credit_order) AS actors,
                   (SELECT group_concat(p.name, char(31)) FROM credit c JOIN person p ON p.id = c.person_id WHERE c.media_id = m.id AND lower(c.department) IN ('directing', 'director') ORDER BY c.credit_order) AS directors,
                   (SELECT group_concat(field, char(31)) FROM (
                       SELECT title_norm AS field FROM search_doc WHERE media_id = m.id
                       UNION ALL SELECT aliases_norm FROM search_doc WHERE media_id = m.id
                       UNION ALL SELECT pinyin FROM search_doc WHERE media_id = m.id
                       UNION ALL SELECT pinyin_alts FROM search_doc WHERE media_id = m.id
                       UNION ALL SELECT initials FROM search_doc WHERE media_id = m.id
                       UNION ALL SELECT initials_alts FROM search_doc WHERE media_id = m.id
                       UNION ALL SELECT people_norm FROM search_doc WHERE media_id = m.id
                       UNION ALL SELECT people_pinyin FROM search_doc WHERE media_id = m.id
                       UNION ALL SELECT genres_norm FROM search_doc WHERE media_id = m.id
                   )) AS search_terms
            FROM media m$selection$sortAndLimit
        """.trimIndent()
        return db.rawQuery(sql, args).use { cursor ->
            buildList {
                while (cursor.moveToNext()) {
                    val externalIds = cursor.getNullableString("external_ids").orEmpty().split(SEPARATOR).mapNotNull { pair ->
                        val delimiter = pair.indexOf(EXTERNAL_SEPARATOR)
                        if (delimiter <= 0) null else pair.substring(0, delimiter) to pair.substring(delimiter + 1)
                    }.toMap()
                    add(
                        CatalogMedia(
                            mytvId = cursor.getString(cursor.getColumnIndexOrThrow("id")),
                            mediaType = cursor.getNullableString("media_type").orEmpty(),
                            title = cursor.getNullableString("title").orEmpty(),
                            originalTitle = cursor.getNullableString("original_title").orEmpty(),
                            year = if (cursor.isNull(cursor.getColumnIndexOrThrow("year"))) "" else cursor.getString(cursor.getColumnIndexOrThrow("year")),
                            updatedAt = cursor.getLong(cursor.getColumnIndexOrThrow("updated_at")),
                            overview = cursor.getNullableString("overview").orEmpty(),
                            posterUrl = cursor.getNullableString("poster_url").orEmpty(),
                            rating = cursor.getNullableDouble("rating") ?: 0.0,
                            popularity = cursor.getDouble(cursor.getColumnIndexOrThrow("popularity")),
                            genres = splitJoined(cursor.getNullableString("genres")),
                            regions = splitJoined(cursor.getNullableString("regions")),
                            externalIds = externalIds,
                            actors = splitJoined(cursor.getNullableString("actors")),
                            directors = splitJoined(cursor.getNullableString("directors")),
                            searchTerms = splitJoined(cursor.getNullableString("search_terms")),
                        ),
                    )
                }
            }
        }
    }

    private fun readSeedCatalog(): List<CatalogMedia> = runCatching {
        val items = JSONObject(seedCatalogJson()).optJSONArray("items") ?: JSONArray()
        (0 until items.length()).mapNotNull { index ->
            val item = items.optJSONObject(index) ?: return@mapNotNull null
            val legacyId = item.optString("d").ifBlank { item.optString("id").removePrefix("douban:") }
            val title = item.optString("t").ifBlank { item.optString("title") }
            if (title.isBlank()) return@mapNotNull null
            val mytvId = item.optString("mytv_id").ifBlank { item.optString("m") }
            val regions = item.optString("ar")
                .takeIf(String::isNotBlank)
                ?.split(" / ")
                ?.filter(String::isNotBlank)
                ?: item.optJSONArray("regions").joinStrings()
            CatalogMedia(
                mytvId = mytvId,
                mediaType = item.optString("c").removePrefix("douban:").ifBlank { item.optString("type") },
                title = title,
                year = item.optString("y").ifBlank { item.optString("year") },
                overview = item.optString("desc").ifBlank { item.optString("summary") },
                posterUrl = item.optString("p").ifBlank { item.optString("poster_url") },
                rating = item.optDouble("r", item.optDouble("rating", 0.0)),
                regions = regions,
                genres = item.optJSONArray("genres").joinStrings(),
                externalIds = if (legacyId.isBlank()) emptyMap() else mapOf("douban" to legacyId),
                searchTerms = (listOf(title, mytvId, item.optString("y"), legacyId) + regions).filter(String::isNotBlank),
            )
        }
    }.getOrDefault(emptyList())

    private fun resolveArtifactUrl(text: String): HttpUrl {
        val resolved = origin.resolve(text) ?: error("invalid artifact URL")
        require(resolved.scheme == origin.scheme && resolved.host == origin.host && resolved.port == origin.port) {
            "metadata artifact URL must stay on the configured origin"
        }
        return resolved
    }

    private fun atomicReplace(staged: File) {
        require(staged.isFile && staged.length() > 0) { "staged catalog is empty" }
        val target = currentFile.toPath()
        val backup = File(directory, "${DATABASE_NAME}.previous")
        backup.delete()
        if (currentFile.isFile) copyAndSync(currentFile, backup)
        var installed = false
        var keepBackup = false
        try {
            atomicMove(staged.toPath(), target)
            installed = true
            syncParentDirectory(directory.toPath())
        } catch (error: Exception) {
            if (installed) {
                try {
                    if (backup.isFile) atomicMove(backup.toPath(), target) else target.toFile().delete()
                    syncParentDirectory(directory.toPath())
                } catch (restoreError: Exception) {
                    keepBackup = true
                    error.addSuppressed(restoreError)
                }
            }
            if (error is AtomicMoveNotSupportedException) {
                throw IllegalStateException("atomic catalog replacement is not supported; previous catalog retained", error)
            }
            throw error
        } finally {
            if (!keepBackup) backup.delete()
        }
    }

    private fun copyAndSync(source: File, target: File) {
        FileInputStream(source).use { input ->
            FileOutputStream(target).use { output ->
                input.copyTo(output)
                output.fd.sync()
            }
        }
    }

    private fun openReadOnly(file: File): SQLiteDatabase =
        SQLiteDatabase.openDatabase(file.absolutePath, null, SQLiteDatabase.OPEN_READONLY)

    private fun scalar(db: SQLiteDatabase, sql: String): String =
        db.rawQuery(sql, null).use { cursor -> if (cursor.moveToFirst()) cursor.getString(0).orEmpty() else "" }

    private fun Cursor.getNullableString(name: String): String? {
        val index = getColumnIndexOrThrow(name)
        return if (isNull(index)) null else getString(index)
    }

    private fun Cursor.getNullableDouble(name: String): Double? {
        val index = getColumnIndexOrThrow(name)
        return if (isNull(index)) null else getDouble(index)
    }

    private fun JSONArray?.toJsonList(): List<JSONObject> =
        (0 until (this?.length() ?: 0)).mapNotNull { this?.optJSONObject(it) }

    private fun JSONArray?.joinStrings(): List<String> =
        (0 until (this?.length() ?: 0)).mapNotNull { this?.optString(it)?.takeIf(String::isNotBlank) }

    private fun splitJoined(value: String?): List<String> = value.orEmpty().split(SEPARATOR).filter(String::isNotBlank)

    private fun normalizeQuery(value: String): String = Normalizer.normalize(value, Normalizer.Form.NFKC)
        .trim().lowercase(Locale.ROOT).replace(Regex("\\s+"), " ")

    private fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it) }

    private data class CatalogManifest(val latestVersion: String, val full: PackageEntry, val deltas: List<DeltaEntry>)
    private data class PackageEntry(
        val version: String,
        val url: String,
        val sha256: String,
        val size: Long,
        val uncompressedSha256: String,
        val uncompressedSize: Long,
    )
    private data class DeltaEntry(val fromVersion: String, val toVersion: String, val url: String, val size: Long, val sha256: String)

    private companion object {
        const val DIRECTORY_NAME = "metadata-catalog"
        const val DATABASE_NAME = "metadata.sqlite"
        const val SEED_ASSET = "catalog_seed.json"
        const val MANIFEST_PATH = "/metadata/manifest.json"
        const val STAGING_DATABASE = "catalog-staging.sqlite"
        const val STAGING_GZIP = "catalog-staging.sqlite.gz.part"
        const val STAGING_DELTA_GZIP = "catalog-staging.delta.sql.gz.part"
        const val STAGING_DELTA_SQL = "catalog-staging.delta.sql.part"
        const val SUPPORTED_SCHEMA = 3
        const val DEFAULT_SEARCH_LIMIT = 60
        const val MAX_SEARCH_LIMIT = 100
        /**
         * Upper bound for the in-memory catalogue. Generous enough that the real catalogue
         * (~565 rows) stays fully resident, while still refusing to materialise a pathological file.
         */
        const val MAX_IN_MEMORY_ROWS = 2_000
        const val SEPARATOR = "\u001f"
        const val EXTERNAL_SEPARATOR = '\u001e'
        const val MAX_COMPRESSED_BYTES = 64L * 1024 * 1024
        const val MAX_DATABASE_BYTES = 128L * 1024 * 1024
        const val MAX_DELTA_BYTES = 32L * 1024 * 1024
        val VERSION_PATTERN = Regex("\\d{10}")
        val SHA256_PATTERN = Regex("[a-f0-9]{64}")
        val REQUIRED_COLUMNS = mapOf(
            "meta" to setOf("key", "value"),
            "media" to setOf("id", "media_type", "title", "year", "overview", "popularity"),
            "external_id" to setOf("media_id", "provider", "external_id"),
            "person" to setOf("id", "name"),
            "credit" to setOf("media_id", "person_id", "department", "credit_order"),
            "genre" to setOf("id", "name"),
            "media_genre" to setOf("media_id", "genre_id"),
            "region" to setOf("id", "name"),
            "media_region" to setOf("media_id", "region_id"),
            "rating" to setOf("media_id", "provider", "score"),
            "image" to setOf("media_id", "image_type", "original_url"),
            "search_doc" to setOf(
                "media_id", "title_norm", "aliases_norm", "pinyin", "pinyin_alts", "initials",
                "initials_alts", "people_norm", "people_pinyin", "genres_norm", "popularity",
            ),
        )
    }
}
