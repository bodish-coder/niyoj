package com.bodish.niyoj

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.webkit.JavascriptInterface
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Collections
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors

/**
 * The Api class from deployer.py, in Kotlin. ui.html calls NiYoj.call(id, method,
 * argsJson); we answer later through __resolve so nothing blocks the UI thread.
 */
class Bridge(private val act: MainActivity) {

    private val io = Executors.newCachedThreadPool()
    private val cfgFile = File(act.filesDir, "apps.json")
    private val ssh = Ssh(File(act.filesDir, "known_hosts"), File(act.filesDir, "ssh"))
    private val busy = Collections.synchronizedSet(mutableSetOf<String>())
    private val stamp = SimpleDateFormat("dd MMM HH:mm", Locale.US)

    init {
        Scripts.load(act)
    }

    @JavascriptInterface
    fun call(id: Int, method: String, argsJson: String) {
        io.execute {
            val result = try {
                encode(dispatch(method, JSONArray(argsJson)))
            } catch (e: Throwable) {
                fail(e.message ?: e.toString()).toString()
            }
            act.js("__resolve($id, ${JSONObject.quote(result)})")
        }
    }

    private fun encode(v: Any?): String = when (v) {
        null -> "null"
        is String -> JSONObject.quote(v)
        is Boolean -> v.toString()
        else -> v.toString()
    }

    private fun dispatch(method: String, a: JSONArray): Any? = when (method) {
        "get_state" -> state()
        "save_server" -> saveServer(a.str(0), a.getString(1), a.getJSONObject(2))
        "delete_server" -> edit { it.getJSONObject("servers").remove(a.getString(0)) }
        "save_app" -> saveApp(a.str(0), a.getString(1), a.getJSONObject(2))
        "delete_app" -> edit { it.getJSONObject("apps").remove(a.getString(0)) }
        "test_conn" -> testConn(a.getJSONObject(0), a.str(1))
        "scan_server" -> scanServer(a.getString(0))
        "import_apps" -> importApps(a.getString(0), a.getJSONArray(1))
        "deploy" -> startDeploy(a.getString(0))
        "read_key" -> Keys.readPub(ssh.keyFile(a.str(0)))
        "gen_key" -> genKey(a.str(0))
        "install_key" -> installKey(a.getJSONObject(0), a.str(1), a.str(2))
        "copy" -> copy(a.getString(0))
        "pick_key", "pick_dir" -> ""              // no file pickers on the phone
        else -> throw IllegalArgumentException("unknown call: $method")
    }

    // JS sends null for omitted arguments; org.json would hand back the text "null"
    private fun JSONArray.str(i: Int): String? =
        if (i >= length() || isNull(i)) null else optString(i).ifEmpty { null }

    private fun ok() = JSONObject().put("ok", true)

    private fun fail(msg: String) = JSONObject().put("ok", false).put("msg", msg)

    // -- config ---------------------------------------------------------
    private fun cfg(): JSONObject {
        val c = if (cfgFile.exists()) JSONObject(cfgFile.readText()) else JSONObject()
        if (!c.has("servers")) c.put("servers", JSONObject())
        if (!c.has("apps")) c.put("apps", JSONObject())
        return c
    }

    private fun save(c: JSONObject) = cfgFile.writeText(c.toString(2))

    private fun edit(change: (JSONObject) -> Unit): JSONObject {
        val c = cfg()
        change(c)
        save(c)
        return ok()
    }

    /** Replace or insert without moving the entry, so the sidebar does not jump. */
    private fun renameKey(d: JSONObject, old: String?, new: String, value: Any): JSONObject {
        val out = JSONObject()
        for (k in d.keys()) if (k == old) out.put(new, value) else out.put(k, d.get(k))
        if (old == null || !d.has(old)) out.put(new, value)
        return out
    }

    private fun state(): JSONObject = cfg().apply {
        put("version", BuildConfig.VERSION_NAME)
        put(
            "caps", JSONObject()
                .put("pick", false)                 // no file or directory picker
                .put("upload", false)               // and so no static-folder upload yet
                .put("install_key", "password")     // sshj does the password hop in-app
                .put("keypath", ssh.keyFile(null as String?).path)
        )
    }

    private fun saveServer(old: String?, name: String, data: JSONObject): JSONObject {
        val c = cfg()
        val servers = c.getJSONObject("servers")
        if (old.isNullOrEmpty() && servers.has(name))
            return fail("A server with that name already exists.")
        servers.optJSONObject(old.orEmpty())?.optJSONObject("last")
            ?.let { data.put("last", it) }                   // keep reachability on edit
        c.put("servers", renameKey(servers, old, name, data))
        if (!old.isNullOrEmpty() && old != name) {
            val apps = c.getJSONObject("apps")
            for (k in apps.keys()) apps.getJSONObject(k).let {
                if (it.optString("server") == old) it.put("server", name)
            }
        }
        save(c)
        return ok()
    }

    private fun saveApp(old: String?, name: String, data: JSONObject): JSONObject {
        val c = cfg()
        val apps = c.getJSONObject("apps")
        if (old.isNullOrEmpty() && apps.has(name))
            return fail("An app with that name already exists.")
        for (k in data.keys().asSequence().toList())
            if (data.optString(k).isEmpty()) data.remove(k)
        apps.optJSONObject(old.orEmpty())?.let { prev ->
            for (k in listOf("last", "commit")) if (!data.has(k) && prev.has(k))
                data.put(k, prev.get(k))
        }
        c.put("apps", renameKey(apps, old, name, data))
        save(c)
        return ok()
    }

    private fun markLast(section: String, name: String, good: Boolean) {
        val c = cfg()
        val entry = c.getJSONObject(section).optJSONObject(name) ?: return
        entry.put(
            "last", JSONObject()
                .put("status", if (good) "ok" else "fail")
                .put("at", stamp.format(Date()))
        )
        save(c)
    }

    // -- actions --------------------------------------------------------
    private fun testConn(server: JSONObject, name: String?): JSONObject {
        if (server.optString("host").isEmpty()) return fail("No host given")
        val out = StringBuilder()
        val code = try {
            ssh.run(server, Scripts.probe) { out.append(it) }
        } catch (e: Exception) {
            name?.let { markLast("servers", it, false) }
            return fail(e.message ?: e.toString())
        }
        val lines = out.lines().map { it.trim() }.filter { it.isNotEmpty() }
        val good = code == 0
        name?.let { markLast("servers", it, good) }
        return JSONObject().put("ok", good).put(
            "msg",
            if (good && lines.isNotEmpty()) lines.takeLast(2).joinToString(" · ")
            else lines.lastOrNull() ?: "ssh exit $code"
        )
    }

    private fun scanServer(name: String): JSONObject {
        val server = cfg().getJSONObject("servers").optJSONObject(name)
            ?: return fail("Unknown server")
        val out = StringBuilder()
        val code = try {
            ssh.run(server, Scripts.scan) { out.append(it) }
        } catch (e: Exception) {
            return fail(e.message ?: e.toString())
        }
        val text = out.toString()
        if (code != 0 && !text.contains("###APPS###"))
            return fail(text.trim().lines().lastOrNull() ?: "ssh exit $code")
        val apps = cfg().getJSONObject("apps")
        val rows = parseScan(text)
        for (i in 0 until rows.length()) {
            val r = rows.getJSONObject(i)
            val known = apps.optJSONObject(r.getString("name"))
            r.put("known", known != null && known.optString("dir") == r.getString("dir"))
        }
        return JSONObject().put("ok", true).put("rows", rows)
    }

    private fun importApps(server: String, rows: JSONArray): JSONObject {
        val c = cfg()
        val apps = c.getJSONObject("apps")
        var added = 0
        var updated = 0
        for (i in 0 until rows.length()) {
            val r = rows.getJSONObject(i)
            val name = r.getString("name")
            val entry = JSONObject()
                .put("server", server)
                .put("repo", r.optString("repo"))
                .put("branch", r.optString("branch").ifEmpty { "main" })
                .put("dir", r.getString("dir"))
                .put("commit", r.optString("commit"))
            val cur = apps.optJSONObject(name)
            when {
                cur != null && cur.optString("dir") == r.getString("dir") -> {
                    for (k in entry.keys()) entry.optString(k).let {
                        if (it.isNotEmpty()) cur.put(k, it)
                    }
                    updated++
                }
                cur != null -> { apps.put("$name ($server)", entry); added++ }
                else -> { apps.put(name, entry); added++ }
            }
        }
        save(c)
        return JSONObject().put("ok", true).put("added", added).put("updated", updated)
    }

    private fun startDeploy(name: String): JSONObject {
        if (!busy.add(name)) return JSONObject().put("ok", false)
        io.execute { runDeploy(name) }
        return ok()
    }

    private fun runDeploy(name: String) {
        val emit = { line: String ->
            act.js("logLine(${JSONObject.quote(name)}, ${JSONObject.quote(line)})")
        }
        var code = 1
        try {
            val c = cfg()
            val app = c.getJSONObject("apps").getJSONObject(name)
            val server = c.getJSONObject("servers").getJSONObject(app.getString("server"))
            if (app.optString("cmd").isEmpty() && app.optString("repo").isEmpty()) {
                emit("=== $name: no repository set - tap Edit and add the Git URL ===\n")
            } else {
                val script = Scripts.deploy(app)
                val user = server.optString("user").ifEmpty { "root" }
                emit("$ ssh $user@${server.optString("host")}  # $name\n")
                script.lines().forEach { emit("| $it\n") }
                emit("-".repeat(62) + "\n")
                code = ssh.run(server, script, onLine = emit)
                emit("\n=== $name: ${if (code == 0) "OK" else "FAILED"} (exit $code) ===\n")
            }
        } catch (e: Exception) {
            emit("\n=== $name: ERROR ${e.message} ===\n")
        } finally {
            busy.remove(name)
            markLast("apps", name, code == 0)
            act.js("deployDone(${JSONObject.quote(name)})")
        }
    }

    // -- keys -----------------------------------------------------------
    private fun genKey(path: String?): JSONObject {
        val priv = ssh.keyFile(path)
        val existed = priv.exists()
        val pub = Keys.generate(priv, "niyoj")
        return JSONObject().put("ok", true).put("existed", existed)
            .put("path", priv.path).put("pub", pub)
    }

    private fun installKey(server: JSONObject, path: String?, password: String?): JSONObject {
        if (server.optString("host").isEmpty()) return fail("No host given")
        if (password.isNullOrEmpty()) return fail("Enter the server password first")
        val priv = ssh.keyFile(path)
        val pub = Keys.generate(priv, "niyoj")
        val out = StringBuilder()
        val code = try {
            ssh.run(server, Scripts.authz(pub), password) { out.append(it) }
        } catch (e: Exception) {
            return fail(e.message ?: e.toString())
        }
        if (code != 0 || !out.contains("NIYOJ_KEY_OK"))
            return fail(out.toString().trim().lines().lastOrNull() ?: "ssh exit $code")
        return JSONObject().put("ok", true).put("how", "done")
            .put("pub", pub).put("path", priv.path)
    }

    private fun copy(text: String): JSONObject {
        val cm = act.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        act.runOnUiThread { cm.setPrimaryClip(ClipData.newPlainText("NiYoj", text)) }
        return ok()
    }

    companion object {
        /** Mirror of parse_scan in deployer.py: rows after the marker, first path wins. */
        fun parseScan(text: String): JSONArray {
            val rows = JSONArray()
            val seen = mutableSetOf<String>()
            var started = false
            for (line in text.lines()) {
                if (line.trim() == "###APPS###") {
                    started = true
                    continue
                }
                if (!started || !line.contains('\t')) continue
                val f = (line.split('\t') + List(9) { "" }).take(9)
                if (!seen.add(f[1])) continue
                rows.put(
                    JSONObject()
                        .put("name", f[0]).put("dir", f[1]).put("repo", f[2])
                        .put("branch", f[3].ifEmpty { "main" }).put("commit", f[4])
                        .put("deploy_sh", f[5] == "1").put("compose", f[6] == "1")
                        .put("up", f[7] == "1").put("domain", f[8])
                )
            }
            return rows
        }
    }
}
