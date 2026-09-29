<#
vncprobe.ps1 - keypress -> first-update latency probe for x11vnc (Windows side)

Measures the round trip from sending a key event to receiving the first
FramebufferUpdate that carries pixels, through whatever transport sits in
between (typically: ssh -L tunnel, WLAN, VDSL). Pairs with bench/keytarget
on the server, which repaints on every keypress so the measured update is
provably the response to the injected key.

Server-side setup (see bench/README.md):
  1. keytarget running:            ./keytarget -g 400x300+64+64 -d 300
  2. throwaway x11vnc, no auth:    x11vnc ... -nopw -localhost -rfbport 5918
                                   -repeat -noipv6 -clip 2560x1440+0+0
                                   -threads -nonap -nocursor
     (-repeat: without it the throwaway server switches off the X server's
     global key auto-repeat for the live session.
     -noipv6: see the IPv6 note below - without it the probe hangs before
     the handshake.)
  3. tunnel from this machine:     ssh -L 5918:127.0.0.1:5918 x11vnc
     (127.0.0.1, *not* localhost - the remote end of the forward resolves
     the name on the server, where ::1 sorts first. The x11vnc Host entry
     in ~/.ssh/config already forwards 5900; the extra -L adds 5918 for
     this throwaway server.)

Then here:
  .\vncprobe.ps1 -Port 5918 -Rect 64,64,400,300
  .\vncprobe.ps1 -Port 5918 -Rect 64,64,400,300 -Trials 20 -Keysym 0xFF56

Notes:
- The server must offer security type None (-nopw); the tunnel keeps it
  local-only.
- Use the keytarget window's rectangle for -Rect so unrelated desktop
  changes (panel clock etc.) can't be mistaken for the key response.
- Keep IPv6 out of the path, on both ends. The ::1 socket may belong to
  libvncserver (fine: its listener thread accepts) or to x11vnc itself
  (broken): x11vnc's copy is accepted only by check_ipv6_listen(), whose
  single call site is rfbPE(), and under -threads watch_loop() never
  reaches rfbPE() while no client is attached. The kernel completes the
  handshake anyway, so TCP connect succeeds and the banner never comes -
  measured 220 s. The throwaway server always lands in the broken case;
  the live 5900 service happens not to. Its startup log says which:
  "Listening also on IPv6 port N (socket 9)" = broken, "Not listening on
  IPv6 interface." = fine. Hence -noipv6 on the server and 127.0.0.1 in
  the -L forward; either one alone is enough. See bench/README.md.
- Numbers include keytarget's own event handling (<1 ms) and the compositor;
  run the server with -wait 2 -defer 2 to see the floor, or with the
  production pacing to see what users get.
#>

param(
    [string]$Computer = '127.0.0.1',
    [int]$Port = 5918,
    [int]$Trials = 10,
    [uint32]$Keysym = 0xFF56,        # Page Down
    [int]$KeyDownMs = 60,
    [int]$TrialTimeoutMs = 3000,
    [int]$QuietMs = 250,             # stream-quiet window for drains
    [string]$Rect = ''               # 'x,y,w,h'; default: full desktop
)

$ErrorActionPreference = 'Stop'

$client = New-Object System.Net.Sockets.TcpClient
try { $client.Connect($Computer, $Port) } catch {
    Write-Error "cannot connect to ${Computer}:${Port} - is the tunnel up? ($($_.Exception.Message))"
    exit 1
}
$client.NoDelay = $true
$stream = $client.GetStream()
$stream.ReadTimeout = 10000          # guard the handshake; drains override it

function Write-Bytes([byte[]]$b) { $stream.Write($b, 0, $b.Length) }

function Read-Exact([int]$n) {
    $buf = New-Object byte[] $n
    $got = 0
    while ($got -lt $n) {
        $r = $stream.Read($buf, $got, $n - $got)
        if ($r -le 0) { throw [System.IO.EndOfStreamException] "connection closed" }
        $got += $r
    }
    return ,$buf
}

function Read-U16 { $b = Read-Exact 2; return ([int]$b[0] -shl 8) -bor [int]$b[1] }
function Read-U32 {
    $b = Read-Exact 4
    return ([long]$b[0] -shl 24) -bor ([long]$b[1] -shl 16) -bor ([long]$b[2] -shl 8) -bor [long]$b[3]
}

function Drain([int]$quietMs, [int]$capMs) {
    # Read until the stream stays quiet for $quietMs (or $capMs total).
    $oldTimeout = $stream.ReadTimeout
    $stream.ReadTimeout = $quietMs
    $scratch = New-Object byte[] 65536
    $total = 0
    $deadline = [DateTime]::UtcNow.AddMilliseconds($capMs)
    try {
        while ([DateTime]::UtcNow -lt $deadline) {
            try {
                $r = $stream.Read($scratch, 0, $scratch.Length)
                if ($r -le 0) { throw [System.IO.EndOfStreamException] "closed" }
                $total += $r
            } catch [System.IO.IOException] {
                break   # read timeout == quiet window elapsed
            }
        }
    } finally { $stream.ReadTimeout = $oldTimeout }
    return $total
}

# ---- RFB 3.8 handshake ----------------------------------------------------
# The banner read is where a misrouted tunnel surfaces: the TCP connect above
# succeeds (so "is the tunnel up?" looks answered) and the server never speaks.
try {
    $banner = [System.Text.Encoding]::ASCII.GetString((Read-Exact 12))
} catch [System.IO.EndOfStreamException] {
    Write-Error "server closed the connection before the RFB banner - is x11vnc still running?"
    exit 1
} catch [System.IO.IOException] {
    Write-Error @"
connected to ${Computer}:${Port} but no RFB banner within $($stream.ReadTimeout) ms.
The TCP path is up, so the tunnel is not the problem - the remote end of the
forward is landing on the server's IPv6 listener, which x11vnc accepts but
does not answer while no client is attached. Fix either end:
  ssh -L ${Port}:127.0.0.1:${Port} ...   (not localhost: it resolves to ::1 first)
  x11vnc ... -noipv6                     (drops the ::1 listener entirely)
"@
    exit 1
}
if (-not $banner.StartsWith('RFB ')) { Write-Error "not an RFB server: $banner"; exit 1 }
Write-Bytes ([System.Text.Encoding]::ASCII.GetBytes("RFB 003.008`n"))

$ntypes = (Read-Exact 1)[0]
if ($ntypes -eq 0) {
    $len = Read-U32
    $reason = [System.Text.Encoding]::UTF8.GetString((Read-Exact ([int]$len)))
    Write-Error "server refused connection: $reason"; exit 1
}
$types = Read-Exact ([int]$ntypes)
if (-not ($types -contains 1)) {
    Write-Error "server requires auth (types: $($types -join ',')); start it with -nopw"
    exit 1
}
Write-Bytes ([byte[]]@(1))                      # security: None
if ((Read-U32) -ne 0) { Write-Error "security handshake failed"; exit 1 }

Write-Bytes ([byte[]]@(1))                      # ClientInit: shared
$width  = Read-U16
$height = Read-U16
$null   = Read-Exact 16                        # pixel format
$nameLen = Read-U32
$name  = [System.Text.Encoding]::UTF8.GetString((Read-Exact ([int]$nameLen)))
Write-Host ("desktop: {0}x{1} name={2}" -f $width, $height, $name)

# ---- big-endian helpers ----------------------------------------------------
# Every element is parenthesised on purpose: PowerShell's comma binds tighter
# than -band, so `($v -shr 8) -band 0xFF, $v -band 0xFF` makes the right-hand
# operand of the first -band an *array* and fails with
#   [System.Object[]] does not contain a method named 'op_BitwiseAnd'
function BE16([long]$v) {
    return [byte[]]@( [byte](($v -shr 8) -band 0xFF), [byte]($v -band 0xFF) )
}
function BE32([long]$v) {
    return [byte[]]@( [byte](($v -shr 24) -band 0xFF), [byte](($v -shr 16) -band 0xFF),
                      [byte](($v -shr 8)  -band 0xFF), [byte]($v -band 0xFF) )
}

# ---- encodings: Tight, Hextile, Raw ---------------------------------------
$enc = @(7, 5, 0)
$msg = [byte[]]@(2, 0) + (BE16 $enc.Count)
foreach ($e in $enc) { $msg += BE32 $e }
Write-Bytes ([byte[]]$msg)

# ---- rectangle of interest ------------------------------------------------
if ($Rect -ne '') {
    # -Rect 64,64,400,300 (unquoted) reaches a [string] param as an *array*
    # joined by $OFS, i.e. "64 64 400 300", so accept commas and whitespace.
    $parts = @($Rect -split '[,\s]+' | Where-Object { $_ -ne '' } | ForEach-Object { [int]$_ })
    if ($parts.Count -ne 4) { Write-Error "-Rect must be x,y,w,h"; exit 1 }
    $rx, $ry, $rw, $rh = $parts
} else { $rx, $ry, $rw, $rh = 0, 0, $width, $height }
Write-Host ("probing rect {0},{1} {2}x{3}, keysym 0x{4:X}, {5} trials" -f $rx, $ry, $rw, $rh, $Keysym, $Trials)

function Send-UpdateRequest([int]$inc) {
    Write-Bytes ([byte[]]( [byte[]]@(3, [byte]$inc) +
        (BE16 $rx) + (BE16 $ry) + (BE16 $rw) + (BE16 $rh) ))
}

function Send-Key([int]$down, [uint32]$keysym) {
    Write-Bytes ([byte[]]( [byte[]]@(4, [byte]$down, 0, 0) + (BE32 $keysym) ))
}

# ---- initial full update of the rect --------------------------------------
Send-UpdateRequest 0
$null = Drain $QuietMs 5000

# ---- trials ----------------------------------------------------------------
$results = New-Object System.Collections.ArrayList
for ($t = 1; $t -le $Trials; $t++) {
    # settle: absorb any pending updates (e.g. previous key release)
    Send-UpdateRequest 1
    $null = Drain $QuietMs 1000

    Send-UpdateRequest 1                 # leave a request pending
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    Send-Key 1 $Keysym
    $sentUp = $false
    $latency = $null

    while ($sw.ElapsedMilliseconds -lt $TrialTimeoutMs) {
        if ((-not $sentUp) -and $sw.ElapsedMilliseconds -ge $KeyDownMs) {
            Send-Key 0 $Keysym
            $sentUp = $true
        }
        $stream.ReadTimeout = 100
        try { $m = $stream.ReadByte() } catch [System.IO.IOException] { continue }
        if ($m -lt 0) { Write-Error "connection closed mid-trial"; exit 1 }
        # the rest of this message may straggle in a later TCP segment over a
        # WAN link; 100 ms is a poll interval, not a message deadline
        $stream.ReadTimeout = 2000
        switch ($m) {
            0 {
                $null = Read-Exact 1     # pad
                $nrects = Read-U16
                if ($nrects -ge 1) { $latency = $sw.Elapsed.TotalMilliseconds }
                # payload is intentionally not parsed; it is drained below
            }
            1 {
                # SetColourMapEntries: pad(1) first(2) ncolours(2) + 6/colour
                $hdr = Read-Exact 5
                $nc = ([int]$hdr[3] -shl 8) -bor [int]$hdr[4]
                $null = Read-Exact ([int]$nc * 6)
            }
            2 { $null = Read-Exact 3 }
            3 { $null = Read-Exact 3; $ln = Read-U32; $null = Read-Exact ([int]$ln) }
            default { Write-Error "unknown server message type $m"; exit 1 }
        }
        if ($null -ne $latency) { break }
    }
    if (-not $sentUp) { Send-Key 0 $Keysym }

    if ($null -eq $latency) {
        Write-Host ("trial {0,2}: FAIL (no update within {1} ms)" -f $t, $TrialTimeoutMs)
        $null = $results.Add($null)
    } else {
        Write-Host ("trial {0,2}: {1,7:N1} ms" -f $t, $latency)
        $null = $results.Add($latency)
    }
    $null = Drain $QuietMs 1000
}

$ok = @($results | Where-Object { $null -ne $_ } | Sort-Object)
if ($ok.Count -eq 0) { Write-Error "no successful trials"; exit 1 }
$med = $ok[[int]($ok.Count / 2)]
$avg = ($ok | Measure-Object -Average).Average
Write-Host ""
Write-Host ("n={0}  min={1:N1}  median={2:N1}  avg={3:N1}  max={4:N1} ms" -f `
    $ok.Count, $ok[0], $med, $avg, $ok[-1])
$client.Close()
