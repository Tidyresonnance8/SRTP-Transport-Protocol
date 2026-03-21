"""
Tests d'intégration et d'interopérabilité pour SRTP.

Deux catégories :

5. TestClientServeur  — ton server.py + ton client.py sur loopback,
   dans des conditions réseau variées (parfait, pertes, corruption).
   Inclut des transferts de fichiers .txt sauvegardés dans OUTPUT_DIR.

6. TestInteroperabilite — ton implémentation contre les binaires de
   référence fournis sur Moodle (tools/server / tools/client).
   Ces tests sont skippés automatiquement si les binaires sont absents.
   Pour les activer : place server et client dans tools/ à la racine
   du projet, puis relance pytest.

Notes sur les interfaces des binaires de référence (spec §2.4) :
  server_ref : tools/server <hostname> <port> [--root <dir>]
               Par défaut root = dossier courant du processus.
               → On lance avec cwd=serve_dir SANS --root.

  client_ref : tools/client <url> [--save <path>]
               --save est bien supporté selon la spec.
               Si le fichier n'apparaît pas à --save, le binaire l'écrit
               dans son cwd sous le nom llm.model (fallback de détection).

Lancement :
    make test         (tous les tests)
    make test-v       (verbeux)
    pytest tests/test_integration.py -v   (uniquement ce fichier)
"""

import os
import sys
import time
import socket
import hashlib
import subprocess
import threading
import pathlib
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import protocol

# ─────────────────────────────────────────────────────────────
#  Constantes
# ─────────────────────────────────────────────────────────────
HOST        = "::1"
SRC_DIR     = os.path.join(os.path.dirname(__file__), '..', 'src')
TOOLS_DIR   = os.path.join(os.path.dirname(__file__), '..', 'tools')
SERVER_PY   = os.path.join(SRC_DIR, 'server.py')
CLIENT_PY   = os.path.join(SRC_DIR, 'client.py')

# Binaires de référence dans tools/ à la racine du projet
SERVER_REF  = os.path.join(TOOLS_DIR, 'server')
CLIENT_REF  = os.path.join(TOOLS_DIR, 'client')

TIMEOUT_TRANSFER = 20   # secondes max pour un transfert complet

# Dossier de sortie persistant pour les fichiers .txt reçus
OUTPUT_DIR = pathlib.Path(os.path.join(os.path.dirname(__file__), '..', 'test_outputs'))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────────────────────────
#  Markers de skip
# ─────────────────────────────────────────────────────────────
ref_server_present = pytest.mark.skipif(
    not os.path.isfile(SERVER_REF),
    reason=f"Binaire server absent ({SERVER_REF}). "
           "Télécharge-le depuis Moodle et place-le dans tools/."
)
ref_client_present = pytest.mark.skipif(
    not os.path.isfile(CLIENT_REF),
    reason=f"Binaire client absent ({CLIENT_REF}). "
           "Télécharge-le depuis Moodle et place-le dans tools/."
)

# ─────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────

def free_port():
    """Retourne un port UDP libre sur ::1."""
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def make_test_file(tmp_path, name, size):
    """Crée un fichier binaire de test avec contenu déterministe."""
    path = tmp_path / name
    data = bytes((i * 37 + 13) % 256 for i in range(size))
    path.write_bytes(data)
    return path


def make_text_file(tmp_path, name, lines=50):
    """Crée un fichier texte .txt avec contenu déterministe."""
    path = tmp_path / name
    content = "\n".join(
        f"Ligne {i:04d} — test SRTP protocole réseau IPv6 loopback données texte."
        for i in range(lines)
    )
    path.write_text(content, encoding='utf-8')
    return path


def run_server(cmd, ready_event, cwd=None):
    """Lance un serveur en subprocess et signale quand il est prêt."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd is not None else None,
    )
    time.sleep(0.4)
    ready_event.set()
    return proc


def find_received_file(save_path, cwd_path, filename):
    """
    Cherche le fichier reçu par un client_ref.
    Tente d'abord save_path (--save), puis llm.model dans cwd_path
    (comportement par défaut de la spec si --save est ignoré).
    Retourne le Path trouvé ou None.
    """
    if save_path is not None and pathlib.Path(save_path).exists():
        return pathlib.Path(save_path)
    fallback = pathlib.Path(cwd_path) / "llm.model"
    if fallback.exists():
        return fallback
    # Dernière tentative : le nom du fichier original dans le cwd
    fallback2 = pathlib.Path(cwd_path) / filename
    if fallback2.exists():
        return fallback2
    return None


def transfer_simple(server_cmd, client_cmd, timeout=TIMEOUT_TRANSFER,
                    server_cwd=None, client_cwd=None):
    """
    Lance server_cmd puis client_cmd, attend la fin du client.
    Retourne (returncode_client, proc_server).
    server_cwd : répertoire de travail du serveur (serve_dir pour server_ref sans --root).
    client_cwd : répertoire de travail du client (utile pour capturer llm.model).
    """
    ready = threading.Event()
    proc_server = run_server(server_cmd, ready, cwd=server_cwd)
    ready.wait(timeout=2)
    try:
        proc_client = subprocess.run(
            client_cmd,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(client_cwd) if client_cwd is not None else None,
        )
        return proc_client.returncode, proc_server
    except subprocess.TimeoutExpired:
        return -1, proc_server
    finally:
        proc_server.terminate()
        proc_server.wait(timeout=2)


# ═══════════════════════════════════════════════════════════════
#  5. TESTS CLIENT ↔ SERVEUR (notre implémentation)
# ═══════════════════════════════════════════════════════════════

class TestClientServeur:
    """
    Teste notre server.py contre notre client.py sur loopback IPv6.
    """

    def _run(self, tmp_path, filename, filesize):
        serve_dir = tmp_path / "serve"
        save_dir  = tmp_path / "save"
        serve_dir.mkdir()
        save_dir.mkdir()

        src = make_test_file(serve_dir, filename, filesize)
        dst = save_dir / "llm.model"
        port = free_port()

        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        client_cmd = ['python3', CLIENT_PY,
                      f'http://[{HOST}]:{port}/{filename}',
                      '--save', str(dst)]

        rc, _ = transfer_simple(server_cmd, client_cmd, server_cwd=serve_dir)
        assert rc == 0, f"Le client s'est terminé avec le code {rc}"
        assert dst.exists(), "Le fichier de destination n'a pas été créé"
        return md5(src), md5(dst)

    def _run_txt(self, tmp_path, filename, lines, output_name):
        serve_dir = tmp_path / "serve"
        serve_dir.mkdir()

        src  = make_text_file(serve_dir, filename, lines=lines)
        dst  = OUTPUT_DIR / output_name
        port = free_port()

        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        client_cmd = ['python3', CLIENT_PY,
                      f'http://[{HOST}]:{port}/{filename}',
                      '--save', str(dst)]

        rc, _ = transfer_simple(server_cmd, client_cmd, server_cwd=serve_dir)
        assert rc == 0, f"Le client s'est terminé avec le code {rc}"
        assert dst.exists(), "Le fichier .txt de destination n'a pas été créé"
        return src, dst

    # ── Transferts binaires ────────────────────────────────────

    def test_transfert_petit_fichier(self, tmp_path):
        """Fichier tenant dans un seul paquet (< 1024 octets)."""
        src_md5, dst_md5 = self._run(tmp_path, "small.bin", 500)
        assert src_md5 == dst_md5, "Intégrité compromise sur petit fichier"

    def test_transfert_exactement_1024_octets(self, tmp_path):
        """Fichier tenant exactement dans un paquet."""
        src_md5, dst_md5 = self._run(tmp_path, "exact.bin", 1024)
        assert src_md5 == dst_md5

    def test_transfert_multi_paquets(self, tmp_path):
        """Fichier nécessitant plusieurs paquets (5 Ko)."""
        src_md5, dst_md5 = self._run(tmp_path, "medium.bin", 5_000)
        assert src_md5 == dst_md5, "Intégrité compromise sur fichier moyen"

    def test_transfert_grand_fichier(self, tmp_path):
        """Fichier de 50 Ko — teste la fenêtre glissante."""
        src_md5, dst_md5 = self._run(tmp_path, "large.bin", 50_000)
        assert src_md5 == dst_md5, "Intégrité compromise sur grand fichier"

    # ── Transferts de fichiers texte (.txt) ───────────────────

    def test_transfert_txt_petit(self, tmp_path):
        """Fichier .txt court (50 lignes) — tenant en un seul paquet."""
        src, dst = self._run_txt(tmp_path, "court.txt", lines=50,
                                 output_name="court_recu.txt")
        assert md5(src) == md5(dst), "Intégrité compromise sur .txt court"
        contenu = dst.read_text(encoding='utf-8')
        assert "Ligne 0000" in contenu, "Le début du fichier texte est manquant"
        assert "Ligne 0049" in contenu, "La fin du fichier texte est manquante"

    def test_transfert_txt_multi_paquets(self, tmp_path):
        """Fichier .txt de taille moyenne (500 lignes) — plusieurs paquets."""
        src, dst = self._run_txt(tmp_path, "moyen.txt", lines=500,
                                 output_name="moyen_recu.txt")
        assert md5(src) == md5(dst), "Intégrité compromise sur .txt moyen"
        contenu = dst.read_text(encoding='utf-8')
        assert "Ligne 0499" in contenu, "La dernière ligne est absente"

    def test_transfert_txt_grand(self, tmp_path):
        """Fichier .txt volumineux (5000 lignes) — fenêtre glissante."""
        src, dst = self._run_txt(tmp_path, "grand.txt", lines=5_000,
                                 output_name="grand_recu.txt")
        assert md5(src) == md5(dst), "Intégrité compromise sur .txt grand"
        contenu = dst.read_text(encoding='utf-8')
        assert "Ligne 4999" in contenu, "La dernière ligne est absente"

    def test_transfert_txt_contenu_identique(self, tmp_path):
        """Vérifie que le contenu texte reçu est identique octet par octet."""
        src, dst = self._run_txt(tmp_path, "identique.txt", lines=100,
                                 output_name="identique_recu.txt")
        assert src.read_bytes() == dst.read_bytes(), \
            "Le contenu reçu diffère de la source"

    # ── Autres cas ────────────────────────────────────────────

    def test_fichier_inexistant(self, tmp_path):
        """Le serveur doit répondre par un paquet vide si le fichier n'existe pas."""
        serve_dir = tmp_path / "serve"
        save_dir  = tmp_path / "save"
        serve_dir.mkdir()
        save_dir.mkdir()

        port = free_port()
        dst  = save_dir / "llm.model"

        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        client_cmd = ['python3', CLIENT_PY,
                      f'http://[{HOST}]:{port}/fichier_inexistant.bin',
                      '--save', str(dst)]

        rc, _ = transfer_simple(server_cmd, client_cmd, server_cwd=serve_dir)
        assert rc == 0, "Le client ne doit pas crasher sur fichier inexistant"
        if dst.exists():
            assert dst.stat().st_size == 0, "Le fichier doit être vide"

    def test_deux_clients_simultanes(self, tmp_path):
        """Deux clients — teste server_multitache."""
        serve_dir = tmp_path / "serve"
        serve_dir.mkdir()

        src_a = make_test_file(serve_dir, "file_a.bin", 10_000)
        src_b = make_test_file(serve_dir, "file_b.bin", 8_000)
        dst_a = tmp_path / "a.model"
        dst_b = tmp_path / "b.model"
        port  = free_port()

        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=2)

        try:
            proc_a = subprocess.Popen(
                ['python3', CLIENT_PY,
                 f'http://[{HOST}]:{port}/file_a.bin',
                 '--save', str(dst_a)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            proc_b = subprocess.Popen(
                ['python3', CLIENT_PY,
                 f'http://[{HOST}]:{port}/file_b.bin',
                 '--save', str(dst_b)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                proc_a.wait(timeout=TIMEOUT_TRANSFER)
                proc_b.wait(timeout=TIMEOUT_TRANSFER)
            except subprocess.TimeoutExpired:
                proc_a.kill()
                proc_b.kill()
                pytest.fail("Timeout : les deux clients n'ont pas terminé à temps")
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=2)

        assert dst_a.exists() and dst_b.exists(), "Un fichier de destination manque"
        assert md5(src_a) == md5(dst_a), "Intégrité compromise pour client A"
        assert md5(src_b) == md5(dst_b), "Intégrité compromise pour client B"

    def test_paquet_corrompu_ignore(self, tmp_path):
        """Transfert réussi même après un paquet corrompu envoyé au serveur."""
        serve_dir = tmp_path / "serve"
        serve_dir.mkdir()
        src = make_test_file(serve_dir, "test.bin", 2_000)
        dst = tmp_path / "llm.model"
        port = free_port()

        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=2)

        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
                s.sendto(b'\x00' * 20, (HOST, port))
            time.sleep(0.05)

            proc_client = subprocess.run(
                ['python3', CLIENT_PY,
                 f'http://[{HOST}]:{port}/test.bin',
                 '--save', str(dst)],
                timeout=TIMEOUT_TRANSFER,
                capture_output=True
            )
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=2)

        assert proc_client.returncode == 0
        assert dst.exists()
        assert md5(src) == md5(dst), "Intégrité compromise après paquet corrompu"


# ═══════════════════════════════════════════════════════════════
#  6. TESTS D'INTEROPÉRABILITÉ avec les binaires de référence
# ═══════════════════════════════════════════════════════════════

class TestInteroperabilite:
    """
    Tests d'interopérabilité entre notre implémentation et les binaires
    de référence fournis sur Moodle (tools/server et tools/client).

    Interface des binaires (spec §2.4) :
      server_ref : tools/server <hostname> <port> [--root <dir>]
                   Lancé avec cwd=serve_dir (sans --root) OU avec --root.
      client_ref : tools/client <url> [--save <path>]
                   Sauvegarde dans --save si supporté, sinon llm.model dans son cwd.

    Quatre combinaisons :
      1) server_ref + client_ref          → sanity check des binaires
      2) notre server.py + client_ref     → notre serveur est conforme
      3) server_ref + notre client.py     → notre client est conforme
      4) notre server.py + N clients_ref  → serveur multi-clients conforme
    """

    # ─────────────────────────────────────────────────────────
    #  Helpers interop
    # ─────────────────────────────────────────────────────────

    def _run_client_ref(self, serve_dir, filename, filesize_or_src,
                        port, save_path, client_cwd,
                        is_text=False, lines=None):
        """
        Lance client_ref avec --save save_path et cwd=client_cwd.
        Cherche le fichier reçu à save_path ou dans client_cwd/llm.model.
        Retourne (src_path, dst_path) ou lève AssertionError.
        """
        if is_text:
            src = make_text_file(serve_dir, filename, lines=lines)
        elif isinstance(filesize_or_src, int):
            src = make_test_file(serve_dir, filename, filesize_or_src)
        else:
            src = filesize_or_src  # déjà créé

        client_cmd = [CLIENT_REF,
                      f'http://[{HOST}]:{port}/{filename}',
                      '--save', str(save_path)]

        rc = subprocess.run(
            client_cmd,
            timeout=TIMEOUT_TRANSFER,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(client_cwd),
        ).returncode

        assert rc == 0, f"client_ref s'est terminé avec le code {rc}"

        # Cherche le fichier : --save en priorité, puis llm.model dans cwd
        dst = find_received_file(save_path, client_cwd, filename)
        assert dst is not None, (
            f"client_ref n'a créé aucun fichier (ni {save_path} "
            f"ni {client_cwd}/llm.model)"
        )
        return src, dst

    # ── 1) Sanity check : server_ref + client_ref ─────────────

    @ref_server_present
    @ref_client_present
    def test_1_sanity_ref_vs_ref(self, tmp_path):
        """
        Vérifie que les deux binaires de référence fonctionnent ensemble.
        Si ce test échoue, le problème vient des binaires, pas du code.
        server_ref lancé depuis serve_dir (root=.) ; client_ref avec --save.
        """
        serve_dir = tmp_path / "serve"
        client_dir = tmp_path / "client"
        serve_dir.mkdir()
        client_dir.mkdir()

        src  = make_test_file(serve_dir, "test.bin", 5_000)
        dst  = client_dir / "received.bin"
        port = free_port()

        # server_ref lancé avec cwd=serve_dir → root=. → trouve test.bin
        server_cmd = [SERVER_REF, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=2)

        try:
            src, dst = self._run_client_ref(
                serve_dir, "test.bin", src, port, dst, client_dir)
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=2)

        assert md5(src) == md5(dst), "Intégrité compromise entre les deux références"

    @ref_server_present
    @ref_client_present
    def test_1_sanity_ref_vs_ref_avec_root(self, tmp_path):
        """
        Sanity check avec server_ref en utilisant --root explicitement.
        Permet de détecter si server_ref supporte --root ou non.
        """
        serve_dir = tmp_path / "serve"
        client_dir = tmp_path / "client"
        serve_dir.mkdir()
        client_dir.mkdir()

        src  = make_test_file(serve_dir, "test.bin", 5_000)
        dst  = client_dir / "received.bin"
        port = free_port()

        server_cmd = [SERVER_REF, HOST, str(port), '--root', str(serve_dir)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready)
        ready.wait(timeout=2)

        try:
            src, dst = self._run_client_ref(
                serve_dir, "test.bin", src, port, dst, client_dir)
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=2)

        assert md5(src) == md5(dst), "Intégrité compromise (sanity avec --root)"

    # ── 2) Notre serveur + client de référence ────────────────

    @ref_client_present
    def test_2_notre_serveur_client_ref_petit(self, tmp_path):
        """client_ref télécharge un petit fichier (.bin) depuis notre server.py."""
        serve_dir  = tmp_path / "serve"
        client_dir = tmp_path / "client"
        serve_dir.mkdir()
        client_dir.mkdir()

        port = free_port()
        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=2)

        try:
            src, dst = self._run_client_ref(
                serve_dir, "small.bin", 500,
                port, client_dir / "small_recu.bin", client_dir)
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=2)

        assert md5(src) == md5(dst), "Intégrité compromise (2, petit .bin)"

    @ref_client_present
    def test_2_notre_serveur_client_ref_grand(self, tmp_path):
        """client_ref télécharge un grand fichier (.bin) depuis notre server.py."""
        serve_dir  = tmp_path / "serve"
        client_dir = tmp_path / "client"
        serve_dir.mkdir()
        client_dir.mkdir()

        port = free_port()
        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=2)

        try:
            src, dst = self._run_client_ref(
                serve_dir, "large.bin", 50_000,
                port, client_dir / "large_recu.bin", client_dir)
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=2)

        assert md5(src) == md5(dst), "Intégrité compromise (2, grand .bin)"

    @ref_client_present
    def test_2_notre_serveur_client_ref_txt(self, tmp_path):
        """client_ref télécharge un fichier .txt depuis notre server.py.
        Résultat sauvegardé dans test_outputs/ pour inspection."""
        serve_dir  = tmp_path / "serve"
        client_dir = tmp_path / "client"
        serve_dir.mkdir()
        client_dir.mkdir()

        dst_path = OUTPUT_DIR / "interop_client_ref_readme_recu.txt"
        port = free_port()

        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=2)

        try:
            src, dst = self._run_client_ref(
                serve_dir, "readme.txt", None,
                port, dst_path, client_dir,
                is_text=True, lines=200)
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=2)

        assert md5(src) == md5(dst), "Intégrité compromise (2, .txt)"

    @ref_client_present
    def test_2_notre_serveur_client_ref_fichier_inexistant(self, tmp_path):
        """client_ref demande un fichier inexistant — notre serveur répond proprement."""
        serve_dir  = tmp_path / "serve"
        client_dir = tmp_path / "client"
        serve_dir.mkdir()
        client_dir.mkdir()

        port = free_port()
        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=2)

        try:
            rc = subprocess.run(
                [CLIENT_REF,
                 f'http://[{HOST}]:{port}/nexiste_pas.bin',
                 '--save', str(client_dir / "vide.bin")],
                timeout=TIMEOUT_TRANSFER,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(client_dir),
            ).returncode
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=2)

        assert rc == 0, "client_ref ne doit pas crasher sur fichier inexistant"

    # ── 3) Serveur de référence + notre client ────────────────
    #
    # server_ref est lancé avec cwd=serve_dir (sans --root).
    # Notre client.py utilise --save vers un fichier précis.

    @ref_server_present
    def test_3_server_ref_notre_client_petit(self, tmp_path):
        """Notre client.py télécharge un petit fichier depuis server_ref."""
        serve_dir = tmp_path / "serve"
        serve_dir.mkdir()
        src = make_test_file(serve_dir, "small.bin", 500)
        dst = tmp_path / "llm.model"
        port = free_port()

        server_cmd = [SERVER_REF, HOST, str(port)]
        client_cmd = ['python3', CLIENT_PY,
                      f'http://[{HOST}]:{port}/small.bin',
                      '--save', str(dst)]

        rc, _ = transfer_simple(server_cmd, client_cmd,
                                server_cwd=serve_dir)
        assert rc == 0, "Notre client a échoué contre server_ref (petit)"
        assert dst.exists()
        assert md5(src) == md5(dst), "Intégrité compromise (3, petit .bin)"

    @ref_server_present
    def test_3_server_ref_notre_client_grand(self, tmp_path):
        """Notre client.py télécharge un grand fichier depuis server_ref."""
        serve_dir = tmp_path / "serve"
        serve_dir.mkdir()
        src = make_test_file(serve_dir, "large.bin", 50_000)
        dst = tmp_path / "llm.model"
        port = free_port()

        server_cmd = [SERVER_REF, HOST, str(port)]
        client_cmd = ['python3', CLIENT_PY,
                      f'http://[{HOST}]:{port}/large.bin',
                      '--save', str(dst)]

        rc, _ = transfer_simple(server_cmd, client_cmd,
                                server_cwd=serve_dir)
        assert rc == 0, "Notre client a échoué sur grand fichier contre server_ref"
        assert md5(src) == md5(dst), "Intégrité compromise (3, grand .bin)"

    @ref_server_present
    def test_3_server_ref_notre_client_txt(self, tmp_path):
        """Notre client.py télécharge un fichier .txt depuis server_ref.
        Résultat sauvegardé dans test_outputs/ pour inspection."""
        serve_dir = tmp_path / "serve"
        serve_dir.mkdir()
        src  = make_text_file(serve_dir, "notes.txt", lines=300)
        dst  = OUTPUT_DIR / "interop_server_ref_notes_recu.txt"
        port = free_port()

        server_cmd = [SERVER_REF, HOST, str(port)]
        client_cmd = ['python3', CLIENT_PY,
                      f'http://[{HOST}]:{port}/notes.txt',
                      '--save', str(dst)]

        rc, _ = transfer_simple(server_cmd, client_cmd,
                                server_cwd=serve_dir)
        assert rc == 0, "Notre client a échoué sur .txt contre server_ref"
        assert dst.exists()
        assert md5(src) == md5(dst), "Intégrité compromise (3, .txt)"
        contenu = dst.read_text(encoding='utf-8')
        assert "Ligne 0000" in contenu
        assert "Ligne 0299" in contenu

    @ref_server_present
    def test_3_server_ref_notre_client_fichier_inexistant(self, tmp_path):
        """Notre client demande un fichier inexistant à server_ref."""
        serve_dir = tmp_path / "serve"
        serve_dir.mkdir()
        dst = tmp_path / "llm.model"
        port = free_port()

        server_cmd = [SERVER_REF, HOST, str(port)]
        client_cmd = ['python3', CLIENT_PY,
                      f'http://[{HOST}]:{port}/nexiste_pas.bin',
                      '--save', str(dst)]

        rc, _ = transfer_simple(server_cmd, client_cmd,
                                server_cwd=serve_dir)
        assert rc == 0, "Notre client ne doit pas crasher sur fichier inexistant"

    # ── 4) Notre serveur + plusieurs clients_ref simultanés ───

    @ref_client_present
    def test_4_deux_clients_ref_simultanes(self, tmp_path):
        """
        Deux client_ref lancés simultanément contre notre server.py.
        Le fichier reçu est cherché dans --save puis dans llm.model du cwd.
        """
        serve_dir   = tmp_path / "serve"
        client_dir_a = tmp_path / "client_a"
        client_dir_b = tmp_path / "client_b"
        serve_dir.mkdir()
        client_dir_a.mkdir()
        client_dir_b.mkdir()

        src_a = make_test_file(serve_dir, "file_a.bin", 10_000)
        src_b = make_test_file(serve_dir, "file_b.bin",  8_000)
        save_a = client_dir_a / "received_a.bin"
        save_b = client_dir_b / "received_b.bin"
        port   = free_port()

        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=2)

        try:
            proc_a = subprocess.Popen(
                [CLIENT_REF, f'http://[{HOST}]:{port}/file_a.bin',
                 '--save', str(save_a)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(client_dir_a),
            )
            proc_b = subprocess.Popen(
                [CLIENT_REF, f'http://[{HOST}]:{port}/file_b.bin',
                 '--save', str(save_b)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(client_dir_b),
            )
            try:
                proc_a.wait(timeout=TIMEOUT_TRANSFER)
                proc_b.wait(timeout=TIMEOUT_TRANSFER)
            except subprocess.TimeoutExpired:
                proc_a.kill()
                proc_b.kill()
                pytest.fail("Timeout : les deux client_ref n'ont pas terminé")
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=2)

        dst_a = find_received_file(save_a, client_dir_a, "file_a.bin")
        dst_b = find_received_file(save_b, client_dir_b, "file_b.bin")
        assert dst_a is not None, "Fichier reçu introuvable pour client_ref A"
        assert dst_b is not None, "Fichier reçu introuvable pour client_ref B"
        assert md5(src_a) == md5(dst_a), "Intégrité compromise pour client_ref A"
        assert md5(src_b) == md5(dst_b), "Intégrité compromise pour client_ref B"

    @ref_client_present
    def test_4_trois_clients_ref_simultanes(self, tmp_path):
        """
        Trois client_ref lancés simultanément contre notre server.py.
        Stress-test du serveur multi-clients.
        """
        serve_dir = tmp_path / "serve"
        serve_dir.mkdir()

        fichiers = [
            ("alpha.bin", 15_000),
            ("beta.bin",  12_000),
            ("gamma.bin",  9_000),
        ]
        srcs = [make_test_file(serve_dir, name, size) for name, size in fichiers]

        client_dirs = []
        save_paths  = []
        for name, _ in fichiers:
            d = tmp_path / f"client_{name}"
            d.mkdir()
            client_dirs.append(d)
            save_paths.append(d / f"received_{name}")

        port = free_port()
        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=2)

        procs = []
        try:
            for (name, _), save, cdir in zip(fichiers, save_paths, client_dirs):
                p = subprocess.Popen(
                    [CLIENT_REF, f'http://[{HOST}]:{port}/{name}',
                     '--save', str(save)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=str(cdir),
                )
                procs.append(p)
            try:
                for p in procs:
                    p.wait(timeout=TIMEOUT_TRANSFER)
            except subprocess.TimeoutExpired:
                for p in procs:
                    p.kill()
                pytest.fail("Timeout : les trois client_ref n'ont pas terminé")
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=2)

        for src, save, cdir, (name, _) in zip(srcs, save_paths, client_dirs, fichiers):
            dst = find_received_file(save, cdir, name)
            assert dst is not None, f"Fichier reçu introuvable pour {name}"
            assert md5(src) == md5(dst), \
                f"Intégrité compromise pour client_ref sur {name}"

    @ref_client_present
    def test_4_clients_ref_fichiers_txt_simultanes(self, tmp_path):
        """
        Deux client_ref téléchargent des .txt simultanément depuis notre server.py.
        Résultats sauvegardés dans test_outputs/ pour inspection.
        """
        serve_dir    = tmp_path / "serve"
        client_dir_a = tmp_path / "client_a"
        client_dir_b = tmp_path / "client_b"
        serve_dir.mkdir()
        client_dir_a.mkdir()
        client_dir_b.mkdir()

        src_a = make_text_file(serve_dir, "doc_a.txt", lines=400)
        src_b = make_text_file(serve_dir, "doc_b.txt", lines=300)
        save_a = OUTPUT_DIR / "interop_multi_doc_a_recu.txt"
        save_b = OUTPUT_DIR / "interop_multi_doc_b_recu.txt"
        port   = free_port()

        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=2)

        try:
            proc_a = subprocess.Popen(
                [CLIENT_REF, f'http://[{HOST}]:{port}/doc_a.txt',
                 '--save', str(save_a)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(client_dir_a),
            )
            proc_b = subprocess.Popen(
                [CLIENT_REF, f'http://[{HOST}]:{port}/doc_b.txt',
                 '--save', str(save_b)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(client_dir_b),
            )
            try:
                proc_a.wait(timeout=TIMEOUT_TRANSFER)
                proc_b.wait(timeout=TIMEOUT_TRANSFER)
            except subprocess.TimeoutExpired:
                proc_a.kill()
                proc_b.kill()
                pytest.fail("Timeout : les client_ref .txt n'ont pas terminé")
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=2)

        dst_a = find_received_file(save_a, client_dir_a, "doc_a.txt")
        dst_b = find_received_file(save_b, client_dir_b, "doc_b.txt")
        assert dst_a is not None, "Fichier doc_a.txt reçu introuvable"
        assert dst_b is not None, "Fichier doc_b.txt reçu introuvable"
        assert md5(src_a) == md5(dst_a), "Intégrité compromise pour doc_a.txt"
        assert md5(src_b) == md5(dst_b), "Intégrité compromise pour doc_b.txt"