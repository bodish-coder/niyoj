package com.bodish.niyoj

import net.schmizz.sshj.SSHClient
import net.schmizz.sshj.common.SecurityUtils
import net.schmizz.sshj.transport.verification.HostKeyVerifier
import org.json.JSONObject
import java.io.File
import java.security.PublicKey

/** Every SSH hop the app makes. Mirrors ssh_argv/ssh_run in deployer.py. */
class Ssh(private val hostsFile: File, private val keyDir: File) {

    /** Trust on first use, then refuse a change — an unexpected new host key is
     *  the one thing we must not shrug off. */
    private inner class Tofu : HostKeyVerifier {
        override fun verify(hostname: String, port: Int, key: PublicKey): Boolean {
            val id = "[$hostname]:$port"
            val fp = SecurityUtils.getFingerprint(key)
            val seen = hostsFile.takeIf { it.exists() }?.useLines { lines ->
                lines.firstOrNull { it.startsWith("$id ") }?.substringAfter(' ')
            }
            if (seen != null) return seen == fp
            hostsFile.parentFile?.mkdirs()
            hostsFile.appendText("$id $fp\n")
            return true
        }

        override fun findExistingAlgorithms(hostname: String, port: Int): List<String> =
            emptyList()
    }

    fun keyFile(server: JSONObject): File = keyFile(server.optString("key"))

    /** Android has no ~/.ssh, and the UI may still hold a desktop-shaped path,
     *  so only the file name survives. */
    fun keyFile(path: String?): File =
        File(keyDir, File(path.orEmpty().ifEmpty { "id_ed25519" }).name)

    fun connect(server: JSONObject, password: String? = null): SSHClient {
        val client = SSHClient()
        client.addHostKeyVerifier(Tofu())
        client.connectTimeout = 12_000
        client.timeout = 30_000
        val port = server.optInt("port", 22).let { if (it <= 0) 22 else it }
        client.connect(server.optString("host"), port)
        val user = server.optString("user").ifEmpty { "root" }
        if (password.isNullOrEmpty()) {
            val key = keyFile(server)
            require(key.exists()) { "no key at ${key.name} — generate one first" }
            client.authPublickey(user, client.loadKeys(key.path))
        } else {
            client.authPassword(user, password)
        }
        client.useCompression()
        return client
    }

    /** Script goes in on stdin exactly like `ssh host bash -s`, so nothing on the
     *  remote side has to survive a round of shell quoting. */
    fun run(server: JSONObject, script: String, password: String? = null,
            onLine: (String) -> Unit): Int {
        val lock = Any()
        val emit = { line: String -> synchronized(lock) { onLine(line + "\n") } }
        connect(server, password).use { client ->
            client.startSession().use { session ->
                val cmd = session.exec("bash -s")
                cmd.outputStream.use { it.write((script + "\n").toByteArray()) }
                val errs = Thread { cmd.errorStream.bufferedReader().forEachLine(emit) }
                errs.start()
                cmd.inputStream.bufferedReader().forEachLine(emit)
                errs.join()
                cmd.join()
                return cmd.exitStatus ?: -1
            }
        }
    }
}
