# Native SAPI5 JSON-line worker. Run with 64-bit Windows PowerShell 5.1.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'

$script:voice = $null
$script:defaultToken = $null
$script:catalogue = [System.Collections.Generic.List[object]]::new()
$script:byName = [System.Collections.Generic.Dictionary[string, object]]::new([System.StringComparer]::Ordinal)
$script:queue = [System.Collections.Generic.Queue[object]]::new()
$script:active = $null
$script:inputReader = $null
$script:outputWriter = $null
$exitCode = 0

function Write-Protocol {
    param([object]$Message)
    $line = ConvertTo-Json -InputObject $Message -Compress -Depth 5
    $script:outputWriter.WriteLine($line)
}

function Release-Com {
    param([object]$Value)
    if ($null -ne $Value -and [System.Runtime.InteropServices.Marshal]::IsComObject($Value)) {
        try { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($Value) } catch { }
    }
}

function Release-Catalogue {
    param([System.Collections.Generic.List[object]]$Entries)
    foreach ($entry in $Entries) {
        Release-Com $entry.token
    }
}

function Add-Tokens {
    param(
        [object]$Tokens,
        [System.Collections.Generic.List[object]]$Entries,
        [System.Collections.Generic.Dictionary[string, object]]$Names
    )
    for ($index = 0; $index -lt $Tokens.Count; $index++) {
        $token = $null
        try {
            $token = $Tokens.Item($index)
            $name = [string]$token.GetDescription()
            if ([string]::IsNullOrEmpty($name)) {
                throw 'An installed SAPI5 voice has no display name.'
            }
            if (-not $Names.ContainsKey($name)) {
                $entry = [pscustomobject]@{ name = $name; token = $token }
                $Entries.Add($entry)
                $Names.Add($name, $entry)
                # Keep the live token, including tokens supplied by dynamic enumerators.
                $token = $null
            }
        }
        finally {
            Release-Com $token
        }
    }
}

function Update-Catalogue {
    $entries = [System.Collections.Generic.List[object]]::new()
    $names = [System.Collections.Generic.Dictionary[string, object]]::new([System.StringComparer]::Ordinal)
    $classic = $null
    $category = $null
    $oneCore = $null
    $oneCoreKey = $null
    try {
        # Classic failures are real failures, not an empty successful catalogue.
        $classic = $script:voice.GetVoices('', '')
        Add-Tokens $classic $entries $names

        # Presence is optional. Read only the category's existence, never copy keys
        # or reconstruct a token from a registry path.
        $oneCoreKey = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SOFTWARE\Microsoft\Speech_OneCore\Voices')
        if ($null -ne $oneCoreKey) {
            $category = New-Object -ComObject SAPI.SpObjectTokenCategory
            $category.SetId('HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices', $false)
            $oneCore = $category.EnumerateTokens('', '')
            Add-Tokens $oneCore $entries $names
        }

        Release-Catalogue $script:catalogue
        $script:catalogue = $entries
        $script:byName = $names
        $entries = $null
    }
    finally {
        if ($null -ne $entries) { Release-Catalogue $entries }
        Release-Com $oneCore
        Release-Com $category
        Release-Com $classic
        if ($null -ne $oneCoreKey) { $oneCoreKey.Dispose() }
    }
}

function Test-JsonNumber {
    param([object]$Value)
    if (-not ($Value -is [int] -or $Value -is [long] -or $Value -is [double] -or $Value -is [decimal])) {
        return $false
    }
    return (-not [double]::IsNaN([double]$Value) -and -not [double]::IsInfinity([double]$Value))
}

function Stop-Speaks {
    param([object]$Target = $null)
    $cancelled = [System.Collections.Generic.List[object]]::new()
    if ($null -ne $script:active -and ($null -eq $Target -or $script:active.id -eq $Target)) {
        # SVSFlagsAsync | SVSFPurgeBeforeSpeak | SVSFIsNotXML.
        # Purge only the one native turn; unrelated jobs live in our own queue.
        [void]$script:voice.Speak('', 19)
        $cancelled.Add($script:active)
        $script:active = $null
    }
    $kept = [System.Collections.Generic.Queue[object]]::new()
    while ($script:queue.Count -gt 0) {
        $turn = $script:queue.Dequeue()
        if ($null -eq $Target -or $turn.id -eq $Target) {
            $cancelled.Add($turn)
        }
        else {
            $kept.Enqueue($turn)
        }
    }
    $script:queue = $kept
    foreach ($turn in $cancelled) {
        Write-Protocol @{ id = $turn.id; event = 'cancelled' }
    }
}

function Drop-QueuedWarmups {
    if ($script:queue.Count -eq 0) { return }
    $kept = [System.Collections.Generic.Queue[object]]::new()
    while ($script:queue.Count -gt 0) {
        $turn = $script:queue.Dequeue()
        if ($turn.warmup) {
            Write-Protocol @{ id = $turn.id; event = 'cancelled' }
        }
        else {
            $kept.Enqueue($turn)
        }
    }
    $script:queue = $kept
}

function Invoke-CommandLine {
    param([string]$Line)
    $requestId = $null
    try {
        try { $command = ConvertFrom-Json -InputObject $Line -ErrorAction Stop }
        catch { throw 'Invalid JSON command.' }
        if ($null -eq $command -or $command -isnot [pscustomobject]) {
            throw 'A command must be a JSON object.'
        }
        $properties = $command.PSObject.Properties
        if ($null -eq $properties['id'] -or -not (Test-JsonNumber $command.id)) {
            throw 'Command id must be a finite JSON number.'
        }
        $requestId = $command.id
        if ($null -eq $properties['op'] -or $command.op -isnot [string]) {
            throw 'Command op must be a string.'
        }
        switch -CaseSensitive ($command.op) {
            'voices' {
                Update-Catalogue
                $voices = @(
                    foreach ($entry in $script:catalogue) {
                        @{ id = $entry.name; name = $entry.name }
                    }
                )
                Write-Protocol @{ id = $requestId; event = 'voices'; voices = $voices }
            }
            'speak' {
                foreach ($field in @('text', 'voice', 'rate', 'volume')) {
                    if ($null -eq $properties[$field]) {
                        throw 'Speak requires text, voice, rate, and volume.'
                    }
                }
                if ($command.text -isnot [string] -or $command.voice -isnot [string]) {
                    throw 'Speak text and voice must be strings.'
                }
                if (-not (Test-JsonNumber $command.rate) -or $command.rate -lt -10 -or $command.rate -gt 10 -or [math]::Truncate([double]$command.rate) -ne $command.rate) {
                    throw 'Speak rate must be an integer from -10 to 10.'
                }
                if (-not (Test-JsonNumber $command.volume) -or $command.volume -lt 0 -or $command.volume -gt 100 -or [math]::Truncate([double]$command.volume) -ne $command.volume) {
                    throw 'Speak volume must be an integer from 0 to 100.'
                }
                if ($null -ne $properties['interrupt']) {
                    if ($command.interrupt -isnot [bool]) {
                        throw 'Speak interrupt must be a boolean.'
                    }
                    if ($command.interrupt) { Stop-Speaks }
                }
                if ($null -ne $script:active -and $script:active.warmup) {
                    Stop-Speaks $script:active.id
                }
                Drop-QueuedWarmups
                $script:queue.Enqueue([pscustomobject]@{
                    id = $requestId
                    text = $command.text
                    voice = $command.voice
                    rate = [int]$command.rate
                    volume = [int]$command.volume
                    actualVoice = $null
                    started = $false
                    warmup = $false
                })
            }
            'warmup' {
                if ($null -eq $properties['voice'] -or $command.voice -isnot [string]) {
                    throw 'Warmup requires a string voice.'
                }
                $hasRealSpeak = ($null -ne $script:active -and -not $script:active.warmup)
                if (-not $hasRealSpeak) {
                    foreach ($item in $script:queue) {
                        if (-not $item.warmup) { $hasRealSpeak = $true; break }
                    }
                }
                if ($hasRealSpeak) {
                    Write-Protocol @{ id = $requestId; event = 'warmed'; voice = $command.voice }
                }
                else {
                    if ($null -ne $script:active -and $script:active.warmup) {
                        Stop-Speaks $script:active.id
                    }
                    Drop-QueuedWarmups
                    $script:queue.Enqueue([pscustomobject]@{
                        id = $requestId
                        text = '1'
                        voice = $command.voice
                        rate = 10
                        volume = 0
                        actualVoice = $null
                        started = $false
                        warmup = $true
                    })
                }
            }
            'cancel' {
                if ($null -ne $properties['target']) {
                    if (-not (Test-JsonNumber $command.target)) {
                        throw 'Cancel target must be a finite JSON number.'
                    }
                    Stop-Speaks $command.target
                }
                else {
                    Stop-Speaks
                }
                Write-Protocol @{ id = $requestId; event = 'ok' }
            }
            default { throw 'Unknown SAPI5 command op.' }
        }
    }
    catch {
        Write-Protocol @{ id = $requestId; event = 'error'; error = $_.Exception.Message }
    }
}

function Start-NextSpeak {
    while ($null -eq $script:active -and $script:queue.Count -gt 0) {
        $turn = $script:queue.Dequeue()
        try {
            $token = $script:defaultToken
            if (-not [string]::IsNullOrEmpty($turn.voice)) {
                if (-not $script:byName.ContainsKey($turn.voice)) { Update-Catalogue }
                if (-not $script:byName.ContainsKey($turn.voice)) {
                    throw ('The requested SAPI5 voice is not installed: {0}' -f $turn.voice)
                }
                $token = $script:byName[$turn.voice].token
            }
            # An empty selection must restore the OS default, not reuse the
            # custom token assigned by the preceding turn.
            $script:voice.Voice = $token
            $turn.actualVoice = [string]$token.GetDescription()
            $script:voice.Rate = $turn.rate
            $script:voice.Volume = $turn.volume
            # SVSFlagsAsync | SVSFIsNotXML: text is never interpreted as SSML.
            [void]$script:voice.Speak($turn.text, 17)
            $script:active = $turn
        }
        catch {
            Write-Protocol @{ id = $turn.id; event = 'error'; error = $_.Exception.Message }
            continue
        }
    }
}

function Complete-ActiveSpeak {
    if ($null -eq $script:active) { return }
    $done = $script:voice.WaitUntilDone(0)
    $status = $null
    $hresult = 0
    $wordLen = 0
    try {
        $status = $script:voice.Status
        $hresult = [int]$status.LastHResult
        $wordLen = [int]$status.InputWordLength
    }
    finally {
        Release-Com $status
    }
    if (-not $script:active.warmup -and -not $script:active.started -and $hresult -ge 0 -and ($wordLen -gt 0 -or $done)) {
        $script:active.started = $true
        Write-Protocol @{ id = $script:active.id; event = 'started'; voice = $script:active.actualVoice }
    }
    if (-not $done) { return }
    # A successful async Speak call does not guarantee successful synthesis.
    # Inspect the native result after completion, not a previous stream's result
    # while the new one is still starting.
    $turn = $script:active
    $script:active = $null
    if ($hresult -lt 0) {
        $nativeError = [System.Runtime.InteropServices.Marshal]::GetExceptionForHR($hresult)
        $message = 'SAPI5 synthesis failed (0x{0:X8}): {1}' -f ([long]$hresult -band 4294967295), $nativeError.Message
        Write-Protocol @{ id = $turn.id; event = 'error'; error = $message }
    }
    elseif ($turn.warmup) {
        Write-Protocol @{ id = $turn.id; event = 'warmed'; voice = $turn.actualVoice }
    }
    else {
        Write-Protocol @{ id = $turn.id; event = 'done'; voice = $turn.actualVoice }
    }
}

try {
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    [Console]::InputEncoding = $utf8
    [Console]::OutputEncoding = $utf8
    $script:outputWriter = [System.IO.StreamWriter]::new([Console]::OpenStandardOutput(), $utf8)
    $script:outputWriter.AutoFlush = $true
    $script:outputWriter.NewLine = "`n"
    # Console.In is synchronized: its ReadLineAsync can block synchronously.
    # Read the actual stream instead, with exactly one outstanding read.
    $script:inputReader = [System.IO.StreamReader]::new([Console]::OpenStandardInput(), $utf8, $true)
    if (-not [Environment]::Is64BitProcess) {
        throw 'SAPI5 requires 64-bit Windows PowerShell.'
    }
    $script:voice = New-Object -ComObject SAPI.SpVoice
    $script:defaultToken = $script:voice.Voice
    if ($null -eq $script:defaultToken) { throw 'The OS has no default SAPI5 voice.' }
    Update-Catalogue
    Write-Protocol @{ event = 'ready' }
    $readTask = $script:inputReader.ReadLineAsync()
    while ($true) {
        if ($readTask.IsCompleted) {
            $line = $readTask.GetAwaiter().GetResult()
            if ($null -eq $line) {
                Stop-Speaks
                break
            }
            $readTask = $script:inputReader.ReadLineAsync()
            Invoke-CommandLine $line
        }
        Complete-ActiveSpeak
        Start-NextSpeak
        if ($null -eq $script:active) {
            # No idle polling; a new line or EOF wakes the worker.
            [void]$readTask.Wait()
        }
        else {
            # Bound cancellation latency without spinning during playback.
            [void]$readTask.Wait(25)
        }
    }
}
catch {
    $exitCode = 1
    $message = 'SAPI5 worker failed: {0}' -f $_.Exception.Message
    try { Write-Protocol @{ event = 'fatal'; error = $message } } catch { }
    # Detach each request before emitting its terminal result, even if stdout
    # itself has failed because the parent has gone away.
    if ($null -ne $script:active) {
        $turn = $script:active
        $script:active = $null
        try { Write-Protocol @{ id = $turn.id; event = 'error'; error = $message } } catch { }
    }
    while ($script:queue.Count -gt 0) {
        $turn = $script:queue.Dequeue()
        try { Write-Protocol @{ id = $turn.id; event = 'error'; error = $message } } catch { }
    }
}
finally {
    if ($null -ne $script:voice) {
        try { [void]$script:voice.Speak('', 19) } catch { }
    }
    Release-Com $script:voice
    Release-Catalogue $script:catalogue
    Release-Com $script:defaultToken
    if ($null -ne $script:inputReader) {
        try { $script:inputReader.Dispose() } catch { }
    }
    if ($null -ne $script:outputWriter) {
        try { $script:outputWriter.Dispose() } catch { }
    }
}
exit $exitCode
