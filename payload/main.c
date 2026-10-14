/* PS5 Local Storefront helper: a small HTTP server the PC app loads through the console's ELF loader.
 *   GET  /info  {"version", "firmware", "model", "mounts": [{"mount", "total", "free"}]}
 *   POST /quit  stops the helper, so a newer copy can take the port
 * Read-only for now. Build with ./build.ps1; VERSION comes from backend/app/__init__.py. */
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <netinet/in.h>
#include <sys/mount.h>
#include <sys/param.h>
#include <sys/socket.h>
#include <sys/time.h>

#define PORT 7735

typedef struct {
  unsigned long unknown1;
  char str_version[0x1c];
  unsigned int bin_version;
  unsigned long unknown2;
} sce_version_t;

typedef struct {
  char useless1[45];
  char message[3075];
} notify_request_t;

int sceKernelGetProsperoSystemSwVersion(sce_version_t *);
int sceKernelGetHwModelName(char *);
int sceKernelSendNotificationRequest(int, notify_request_t *, size_t, int);

/* The PC app finds the bundled helper's version by searching the .elf for this string. */
__attribute__((used)) static const char VERSION_MARKER[] = "storefront-helper-version:" VERSION;

static char out[65536];
static size_t len;

static void put(const char *fmt, ...) {
  va_list ap;
  va_start(ap, fmt);
  if (len < sizeof out) len += vsnprintf(out + len, sizeof out - len, fmt, ap);
  va_end(ap);
}

static void put_str(const char *s) {
  put("\"");
  for (; *s; s++) {
    if (*s == '"' || *s == '\\') put("\\%c", *s);
    else if ((unsigned char)*s < 0x20) put("\\u%04x", *s);
    else put("%c", *s);
  }
  put("\"");
}

static void info(void) {
  sce_version_t v;
  char model[1000] = "";
  const char *fw = "";
  memset(&v, 0, sizeof v);
  if (!sceKernelGetProsperoSystemSwVersion(&v)) for (fw = v.str_version; *fw == ' '; fw++);
  sceKernelGetHwModelName(model);

  put("{\"version\":");
  put_str(VERSION);
  put(",\"firmware\":");
  put_str(fw);
  put(",\"model\":");
  put_str(model);
  put(",\"mounts\":[");
  int n = getfsstat(NULL, 0, MNT_NOWAIT);
  struct statfs *m = n > 0 ? calloc(n, sizeof *m) : NULL;
  if (m) n = getfsstat(m, n * sizeof *m, MNT_NOWAIT);
  for (int i = 0; m && i < n; i++) {
    put(i ? ",{\"mount\":" : "{\"mount\":");
    put_str(m[i].f_mntonname);
    put(",\"total\":%llu,\"free\":%llu}", (unsigned long long)m[i].f_blocks * m[i].f_bsize,
        (unsigned long long)m[i].f_bavail * m[i].f_bsize);
  }
  free(m);
  put("]}");
}

static void reply(int c, const char *status) {
  char head[256];
  int n = snprintf(head, sizeof head,
                   "HTTP/1.1 %s\r\nContent-Type: application/json\r\nContent-Length: %zu\r\nConnection: close\r\n\r\n",
                   status, len);
  send(c, head, n, 0);
  send(c, out, len, 0);
}

/* Answers one request; returns 1 when the helper should stop. */
static int handle(int c) {
  char req[4096], method[8] = "", path[256] = "";
  size_t got = 0;
  ssize_t r;
  while (got < sizeof req - 1 && (r = recv(c, req + got, sizeof req - 1 - got, 0)) > 0) {
    got += r;
    req[got] = 0;
    if (strstr(req, "\r\n\r\n")) break;
  }
  req[got] = 0;
  sscanf(req, "%7s %255s", method, path);

  len = 0;
  int quit = 0;
  if (!strcmp(method, "GET") && !strcmp(path, "/info")) {
    info();
    reply(c, "200 OK");
  } else if (!strcmp(method, "POST") && !strcmp(path, "/quit")) {
    put("{\"ok\":true}");
    reply(c, "200 OK");
    quit = 1;
  } else {
    put("{\"error\":\"not_found\"}");
    reply(c, "404 Not Found");
  }
  close(c);
  return quit;
}

static void notify(const char *msg) {
  notify_request_t req;
  memset(&req, 0, sizeof req);
  strncpy(req.message, msg, sizeof req.message - 1);
  sceKernelSendNotificationRequest(0, &req, sizeof req, 0);
}

int main(void) {
  signal(SIGPIPE, SIG_IGN);
  int srv = socket(AF_INET, SOCK_STREAM, 0), one = 1;
  setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
  struct sockaddr_in addr;
  memset(&addr, 0, sizeof addr);
  addr.sin_len = sizeof addr;
  addr.sin_family = AF_INET;
  addr.sin_port = htons(PORT);
  addr.sin_addr.s_addr = htonl(INADDR_ANY);
  for (int tries = 0; bind(srv, (struct sockaddr *)&addr, sizeof addr); tries++) {
    if (tries == 20) {  /* an older copy that won't quit holds the port */
      notify("PS5 Local Storefront helper couldn't start: port 7735 is in use.");
      return 1;
    }
    sleep(1);
  }
  listen(srv, 8);
  notify("PS5 Local Storefront helper " VERSION " is running.");

  struct timeval timeout = {5, 0};  /* a client that stops sending can't block the helper */
  for (;;) {
    int c = accept(srv, NULL, NULL);
    if (c < 0) continue;
    setsockopt(c, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof timeout);
    if (handle(c)) break;
  }
  close(srv);
  return 0;
}
