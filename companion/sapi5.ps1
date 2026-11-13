# Native SAPI5 JSON-line worker. Run with 64-bit Windows PowerShell 5.1.
param([string]$OutputDirectory = '')
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
    $line = ConvertTo-Json -InputObject $Message -Compress -Depth 12
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
            $identity = [string]$token.Id
            if (-not ($Entries | Where-Object { $_.identity -ceq $identity })) {
                $entry = [pscustomobject]@{ name = $name; identity = $identity; token = $token }
                $Entries.Add($entry)
                if (-not $Names.ContainsKey($name)) { $Names.Add($name, $entry) }
                if (-not $Names.ContainsKey($identity)) { $Names.Add($identity, $entry) }
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
        $probe = New-Object -ComObject SAPI.SpVoice
        try { $script:defaultToken = $probe.Voice } finally { Release-Com $probe }
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
        $defaultIdentity = [string]$script:defaultToken.Id
        if (-not ($entries | Where-Object { $_.identity -ceq $defaultIdentity })) {
            $defaultName = [string]$script:defaultToken.GetDescription()
            $entry = [pscustomobject]@{ name = $defaultName; identity = $defaultIdentity; token = $script:defaultToken }
            $entries.Add($entry)
            if (-not $names.ContainsKey($defaultName)) { $names.Add($defaultName, $entry) }
            if (-not $names.ContainsKey($defaultIdentity)) { $names.Add($defaultIdentity, $entry) }
        }

        # Queued turns retain the actual live token; let RCW lifetime release replaced catalogues.
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

function Get-ScalarOffsets {
    param([string]$Text)
    $offsets = [System.Collections.Generic.List[int]]::new()
    for ($i = 0; $i -lt $Text.Length; $i++) {
        $offsets.Add($i)
        if ([char]::IsHighSurrogate($Text[$i])) {
            if ($i + 1 -ge $Text.Length -or -not [char]::IsLowSurrogate($Text[$i + 1])) { throw 'Invalid Unicode text.' }
            $i++
        }
        elseif ([char]::IsLowSurrogate($Text[$i])) { throw 'Invalid Unicode text.' }
    }
    $offsets.Add($Text.Length)
    return ,$offsets
}

function Get-Features {
    param([object]$Features)
    if ($null -eq $Features) { return ,@() }
    if ($Features -isnot [array] -or $Features.Count -gt 4) { throw 'Features must be an array.' }
    foreach ($feature in $Features) {
        if ($feature -isnot [string] -or @('rate', 'pitch', 'boundaries', 'pauses') -cnotcontains $feature) {
            throw 'Unknown speech feature.'
        }
    }
    return ,@($Features | Select-Object -Unique)
}

function Compile-Markup {
    param([object]$Markup, [string]$Text, [object[]]$Features, [string]$Language = 'und')
    if ($Markup -isnot [pscustomobject] -or $Markup.text -isnot [string] -or $Markup.text -cne $Text -or
        $Markup.spans -isnot [array] -or $Markup.spans.Count -gt 128) { throw 'Invalid speech markup.' }
    foreach ($property in $Markup.PSObject.Properties.Name) {
        if (@('text', 'spans') -cnotcontains $property) { throw 'Unknown markup property.' }
    }
    $offsets = Get-ScalarOffsets $Text
    $length = $offsets.Count - 1
    $clip = [math]::Min(4000, $length)
    $settings = [System.Xml.XmlWriterSettings]::new()
    $settings.OmitXmlDeclaration = $true
    $buffer = [System.Text.StringBuilder]::new()
    $writer = [System.Xml.XmlWriter]::Create($buffer, $settings)
    try {
        $writer.WriteStartElement('speak', 'http://www.w3.org/2001/10/synthesis')
        $writer.WriteAttributeString('version', '1.0')
        $writer.WriteAttributeString('xml', 'lang', 'http://www.w3.org/XML/1998/namespace', $Language)
        $position = 0
        $previousEnd = 0
        $pauses = 0
        $scope = $false
        foreach ($span in $Markup.spans) {
            if ($span -isnot [pscustomobject]) { throw 'Invalid speech span.' }
            $p = $span.PSObject.Properties
            if ($null -eq $p['start'] -or $null -eq $p['end'] -or
                -not (Test-JsonNumber $span.start) -or -not (Test-JsonNumber $span.end) -or
                [math]::Truncate([double]$span.start) -ne $span.start -or
                [math]::Truncate([double]$span.end) -ne $span.end -or
                $span.start -lt $previousEnd -or $span.end -le $span.start -or $span.end -gt $length) { throw 'Invalid speech span offsets.' }
            foreach ($key in $p.Name) {
                if (@('start', 'end', 'rate', 'pitch', 'boundary', 'after_ms') -cnotcontains $key) { throw 'Unknown span property.' }
            }
            foreach ($key in @('rate', 'pitch')) {
                if ($null -ne $p[$key]) {
                    $values = if ($key -eq 'rate') { @('slow', 'medium', 'fast') } else { @('low', 'medium', 'high') }
                    if ($Features -cnotcontains $key -or $values -cnotcontains $span.$key) { throw 'Disallowed prosody effect.' }
                }
            }
            if ($null -ne $p['boundary'] -and ($Features -cnotcontains 'boundaries' -or @('s', 'p') -cnotcontains $span.boundary)) { throw 'Disallowed boundary.' }
            if ($null -ne $p['after_ms']) {
                if ($Features -cnotcontains 'pauses' -or -not (Test-JsonNumber $span.after_ms) -or
                    [math]::Truncate([double]$span.after_ms) -ne $span.after_ms -or $span.after_ms -lt 0 -or $span.after_ms -gt 1500) { throw 'Disallowed native pause.' }
                $pauses += $span.after_ms
                if ($pauses -gt 3000) { throw 'Native pause budget exceeded.' }
            }
            $previousEnd = [int]$span.end
            # A clipped target is dropped whole; it never moves to another occurrence.
            if ($span.end -gt $clip) { continue }
            if (-not $scope) {
                foreach ($following in $Markup.spans) {
                    if ($following.end -lt $span.end -or $following.end -gt $clip) { continue }
                    if ($null -ne $following.PSObject.Properties['boundary']) {
                        $writer.WriteStartElement([string]$following.boundary)
                        $scope = $true
                        break
                    }
                }
            }
            $writer.WriteString($Text.Substring($offsets[$position], $offsets[[int]$span.start] - $offsets[$position]))
            $prosody = $null -ne $p['rate'] -or $null -ne $p['pitch']
            if ($prosody) {
                $writer.WriteStartElement('prosody')
                foreach ($key in @('rate', 'pitch')) { if ($null -ne $p[$key]) { $writer.WriteAttributeString($key, [string]$span.$key) } }
            }
            $writer.WriteString($Text.Substring($offsets[[int]$span.start], $offsets[[int]$span.end] - $offsets[[int]$span.start]))
            if ($prosody) { $writer.WriteEndElement() }
            if ($null -ne $p['after_ms']) {
                $writer.WriteStartElement('break')
                $writer.WriteAttributeString('time', ('{0}ms' -f $span.after_ms))
                $writer.WriteEndElement()
            }
            if ($null -ne $p['boundary']) { $writer.WriteEndElement(); $scope = $false }
            $position = [int]$span.end
        }
        $writer.WriteString($Text.Substring($offsets[$position], $offsets[$clip] - $offsets[$position]))
        $writer.WriteEndElement()
        $writer.Flush()
        return $buffer.ToString()
    }
    finally { $writer.Dispose() }
}

function Read-ManualNode {
    param([object]$Node, [object]$State, [hashtable]$Effects, [int]$Depth)
    if ($Depth -gt 8) { throw 'SSML nesting limit exceeded.' }
    if ($Node -is [System.Xml.XmlText] -or $Node -is [System.Xml.XmlWhitespace] -or $Node -is [System.Xml.XmlSignificantWhitespace]) {
        $start = $State.length
        [void]$State.text.Append($Node.Value)
        $State.length += (Get-ScalarOffsets $Node.Value).Count - 1
        if ($State.length -gt 4000) { throw 'SSML text limit exceeded.' }
        if ($Node.Value.Length -gt 0) {
            $span = @{ start = $start; end = $State.length }
            foreach ($key in $Effects.Keys) { $span[$key] = $Effects[$key] }
            $State.spans.Add([pscustomobject]$span)
            # Nonempty runs are bounded by the 4000-scalar text limit. Keep plain
            # runs until enclosing scopes and following breaks attach their effects.
        }
        return
    }
    if ($Node -isnot [System.Xml.XmlElement] -or $Node.NamespaceURI -cne 'http://www.w3.org/2001/10/synthesis') { throw 'Unsupported SSML content.' }
    $name = $Node.LocalName
    if (@('prosody', 's', 'p', 'break') -cnotcontains $name) { throw 'Unsupported SSML control.' }
    if (($name -eq 's' -or $name -eq 'p') -and $State.features -cnotcontains 'boundaries') { throw 'Disallowed boundary.' }
    if ($name -eq 'break' -and $State.features -cnotcontains 'pauses') { throw 'Disallowed native pause.' }
    $next = @{}
    foreach ($key in $Effects.Keys) { $next[$key] = $Effects[$key] }
    foreach ($attribute in $Node.Attributes) {
        if ($attribute.NamespaceURI -ne '' -or $name -ne 'prosody' -or @('rate', 'pitch') -cnotcontains $attribute.LocalName) {
            if (-not ($name -eq 'break' -and $attribute.Name -ceq 'time')) { throw 'Unsupported SSML attribute.' }
        }
        if ($name -eq 'prosody') {
            $key = $attribute.LocalName
            $values = if ($key -eq 'rate') { @('slow', 'medium', 'fast') } else { @('low', 'medium', 'high') }
            if ($State.features -cnotcontains $key -or $values -cnotcontains $attribute.Value) { throw 'Disallowed prosody effect.' }
            $next[$key] = $attribute.Value
        }
    }
    if ($name -eq 'break') {
        if ($Node.Attributes.Count -ne 1 -or $Node.HasChildNodes -or $State.spans.Count -eq 0 -or
            $Node.GetAttribute('time') -cnotmatch '^([0-9]{1,4})(ms|s)$') { throw 'Invalid native break.' }
        $ms = [int]$Matches[1]
        if ($Matches[2] -eq 's') { $ms *= 1000 }
        $last = $State.spans[$State.spans.Count - 1]
        if ($null -ne $last.PSObject.Properties['after_ms']) { throw 'Multiple breaks at one boundary.' }
        $last | Add-Member -NotePropertyName after_ms -NotePropertyValue $ms
        return
    }
    if ($name -eq 'prosody' -and $Node.Attributes.Count -eq 0) { throw 'Prosody requires rate or pitch.' }
    $before = $State.spans.Count
    foreach ($child in $Node.ChildNodes) { Read-ManualNode $child $State $next ($Depth + 1) }
    if ($State.spans.Count -eq $before) { throw 'Empty speech scope.' }
    if ($name -eq 's' -or $name -eq 'p') {
        # A boundary marks the following sentence/paragraph end, not every prosody run.
        # Coincident paragraph/sentence endings retain the stronger paragraph boundary.
        $last = $State.spans[$State.spans.Count - 1]
        if ($null -eq $last.PSObject.Properties['boundary'] -or $name -eq 'p') {
            $last | Add-Member -NotePropertyName boundary -NotePropertyValue $name -Force
        }
    }
}

function Prepare-Manual {
    param([object]$Ssml, [object[]]$Features)
    if ($Ssml -isnot [string] -or $Ssml.Length -eq 0 -or [System.Text.Encoding]::UTF8.GetByteCount($Ssml) -gt 32768 -or
        $Ssml -match '<[!?]') { throw 'SSML must be a bounded document without declarations or processing instructions.' }
    $settings = [System.Xml.XmlReaderSettings]::new()
    $settings.DtdProcessing = [System.Xml.DtdProcessing]::Prohibit
    $settings.XmlResolver = $null
    $settings.MaxCharactersInDocument = 32768
    $reader = [System.Xml.XmlReader]::Create([System.IO.StringReader]::new($Ssml), $settings)
    try {
        $doc = [System.Xml.XmlDocument]::new()
        $doc.XmlResolver = $null
        $doc.PreserveWhitespace = $true
        $doc.Load($reader)
        $root = $doc.DocumentElement
        if ($root.LocalName -cne 'speak' -or $root.NamespaceURI -cne 'http://www.w3.org/2001/10/synthesis' -or
            $root.GetAttribute('version') -cne '1.0' -or
            $root.GetAttribute('lang', 'http://www.w3.org/XML/1998/namespace') -cnotmatch '^[a-zA-Z]{2,8}(-[a-zA-Z0-9]{1,8})*$') { throw 'Invalid W3C speak envelope.' }
        foreach ($attribute in $root.Attributes) {
            if ($attribute.Name -cnotin @('xmlns', 'version', 'xml:lang')) { throw 'Unsupported speak metadata.' }
        }
        $state = @{ text = [System.Text.StringBuilder]::new(); length = 0; features = $Features
                    spans = [System.Collections.Generic.List[object]]::new() }
        foreach ($child in $root.ChildNodes) { Read-ManualNode $child $state @{} 1 }
        if ([string]::IsNullOrWhiteSpace($state.text.ToString())) { throw 'SSML has no speech text.' }
        $spans = @(
            foreach ($span in $state.spans) {
                $properties = $span.PSObject.Properties
                if ($null -ne $properties['rate'] -or $null -ne $properties['pitch'] -or
                    $null -ne $properties['boundary'] -or $null -ne $properties['after_ms']) { $span }
            }
        )
        if ($spans.Count -gt 128) { throw 'SSML span limit exceeded.' }
        $markup = [pscustomobject]@{ text = $state.text.ToString(); spans = $spans }
        [void](Compile-Markup $markup $markup.text $Features)
        return $markup
    }
    finally { $reader.Dispose() }
}

function Get-TokenLanguage {
    param([object]$Token)
    try {
        $language = ([string]$Token.GetAttribute('Language')).Split(';')[0]
        return [System.Globalization.CultureInfo]::GetCultureInfo([Convert]::ToInt32($language, 16)).Name
    }
    catch { return 'und' }
}

function Resolve-Token {
    param([string]$Selection)
    if ([string]::IsNullOrEmpty($Selection)) {
        $probe = New-Object -ComObject SAPI.SpVoice
        try { return $probe.Voice } finally { Release-Com $probe }
    }
    if (-not $script:byName.ContainsKey($Selection)) { Update-Catalogue }
    if (-not $script:byName.ContainsKey($Selection)) { throw ('The requested SAPI5 voice is not installed: {0}' -f $Selection) }
    return $script:byName[$Selection].token
}

function Test-JsonNumber {
    param([object]$Value)
    if (-not ($Value -is [int] -or $Value -is [long] -or $Value -is [double] -or $Value -is [decimal])) {
        return $false
    }
    return (-not [double]::IsNaN([double]$Value) -and -not [double]::IsInfinity([double]$Value))
}

function Stop-AudioObservation {
    param([object]$Turn)
    if ($null -eq $Turn.audioObservation) { return }
    Release-Com $Turn.audioObservation.stream
    $Turn.audioObservation = $null
}

function Get-AudioWritePosition {
    param([object]$Stream)
    $status = $null
    try {
        $status = $Stream.Status
        $position = $status.CurrentSeekPosition
        if (-not ((Test-JsonNumber $position) -or $position -is [uint] -or $position -is [ulong]) -or $position -lt 0) {
            throw 'The audio write cursor is unavailable.'
        }
        return [decimal]$position
    }
    finally { Release-Com $status }
}

function Start-AudioObservation {
    param([object]$Turn)
    if ($Turn.warmup -or $null -ne $Turn.output) { return }
    $stream = $null
    try {
        # Read the existing default output only; never replace or seek it.
        $stream = $script:voice.AudioOutputStream
        $position = Get-AudioWritePosition $stream
        $Turn.audioObservation = [pscustomobject]@{
            stream = $stream
            position = $position
            first = $null
            last = $null
        }
        $stream = $null
    }
    catch { }
    finally { Release-Com $stream }
}

function Update-AudioObservation {
    param([object]$Turn)
    if ($null -eq $Turn.audioObservation) { return }
    try {
        $observation = $Turn.audioObservation
        $position = Get-AudioWritePosition $observation.stream
        if ($position -lt $observation.position) {
            # A reset loses continuity for the whole turn, even after an advance.
            Stop-AudioObservation $Turn
            return
        }
        if ($position -gt $observation.position) {
            $now = [System.Diagnostics.Stopwatch]::GetTimestamp()
            if ($null -eq $observation.first) { $observation.first = $now }
            $observation.last = $now
            $observation.position = $position
        }
    }
    catch {
        # Optional diagnostics must never change speech or playback lifecycle.
        Stop-AudioObservation $Turn
    }
}

function Stop-Speaks {
    param([object]$Target = $null)
    $cancelled = [System.Collections.Generic.List[object]]::new()
    if ($null -ne $script:active -and ($null -eq $Target -or $script:active.id -eq $Target)) {
        # SVSFlagsAsync | SVSFPurgeBeforeSpeak | SVSFIsNotXML.
        # Purge only the one native turn; unrelated jobs live in our own queue.
        Stop-AudioObservation $script:active
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
        if ($null -ne $turn.output) {
            $turn.output.Close()
            $script:voice.AudioOutputStream = $null
            Release-Com $turn.output
            $turn.output = $null
        }
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
            'prepare' {
                if ($null -eq $properties['ssml']) { throw 'Prepare requires ssml.' }
                $features = if ($null -ne $properties['features']) { Get-Features $command.features } else { @() }
                $markup = Prepare-Manual $command.ssml $features
                Write-Protocol @{ id = $requestId; event = 'prepared'; markup = $markup }
            }
            'voices' {
                Update-Catalogue
                $voices = @(
                    foreach ($entry in $script:catalogue) {
                        @{ id = $entry.name; name = $entry.name; identity = $entry.identity
                           isDefault = ($entry.identity -ceq [string]$script:defaultToken.Id)
                           evidence = @{ rate = 'unknown'; pitch = 'unknown'; boundaries = 'unknown'; pauses = 'unknown' } }
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
                $features = if ($null -ne $properties['features']) { Get-Features $command.features } else { @() }
                $offsets = Get-ScalarOffsets $command.text
                $content = $command.text.Substring(0, $offsets[[math]::Min(4000, $offsets.Count - 1)])
                $flags = 17
                $token = Resolve-Token $command.voice
                if ($null -ne $properties['markup']) {
                    $content = Compile-Markup $command.markup $command.text $features (Get-TokenLanguage $token)
                    $flags = 265
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
                    token = $token
                    content = $content
                    flags = $flags
                    output = $null
                    audioObservation = $null
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
                        token = (Resolve-Token $command.voice)
                        content = '1'
                        flags = 17
                        output = $null
                        audioObservation = $null
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
            $token = $turn.token
            $script:voice.Voice = $token
            $turn.actualVoice = [string]$token.GetDescription()
            $script:voice.Rate = $turn.rate
            $script:voice.Volume = $turn.volume
            if ($OutputDirectory -and -not $turn.warmup) {
                $stream = New-Object -ComObject SAPI.SpFileStream
                try {
                    $stream.Open((Join-Path $OutputDirectory ('{0}.wav' -f $turn.id)), 3, $false)
                    $script:voice.AudioOutputStream = $stream
                    $turn.output = $stream
                }
                catch { Release-Com $stream; throw }
            }
            Start-AudioObservation $turn
            # Plain=17; only the escaped, validated W3C document uses async SSML=265.
            [void]$script:voice.Speak($turn.content, $turn.flags)
            $script:active = $turn
        }
        catch {
            Stop-AudioObservation $turn
            if ($null -ne $turn.output) {
                try { $turn.output.Close(); $script:voice.AudioOutputStream = $null } finally { Release-Com $turn.output }
                $turn.output = $null
            }
            Write-Protocol @{ id = $turn.id; event = 'error'; error = $_.Exception.Message }
            continue
        }
    }
}

function Complete-ActiveSpeak {
    if ($null -eq $script:active) { return }
    Update-AudioObservation $script:active
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
        Write-Protocol @{ id = $script:active.id; event = 'started'; voice = $script:active.actualVoice
                          word_boundary = ($wordLen -gt 0) }
    }
    if (-not $done) { return }
    # A successful async Speak call does not guarantee successful synthesis.
    # Inspect the native result after completion, not a previous stream's result
    # while the new one is still starting.
    $turn = $script:active
    $script:active = $null
    $observation = $turn.audioObservation
    Stop-AudioObservation $turn
    if ($null -ne $turn.output) {
        $turn.output.Close()
        $script:voice.AudioOutputStream = $null
        Release-Com $turn.output
        $turn.output = $null
    }
    if ($hresult -lt 0) {
        $nativeError = [System.Runtime.InteropServices.Marshal]::GetExceptionForHR($hresult)
        $message = 'SAPI5 synthesis failed (0x{0:X8}): {1}' -f ([long]$hresult -band 4294967295), $nativeError.Message
        Write-Protocol @{ id = $turn.id; event = 'error'; error = $message }
    }
    elseif ($turn.warmup) {
        Write-Protocol @{ id = $turn.id; event = 'warmed'; voice = $turn.actualVoice }
    }
    else {
        $completion = @{ id = $turn.id; event = 'done'; voice = $turn.actualVoice }
        if ($turn.flags -eq 265) { $completion.ssml_document = $turn.content }
        if ($null -ne $observation -and $null -ne $observation.first) {
            # Retrospective, approximate write-delivery ages, not playback EOF.
            $completedAt = [System.Diagnostics.Stopwatch]::GetTimestamp()
            $completion.first_audio_ago_ms = [long][math]::Round(($completedAt - $observation.first) * 1000.0 / [System.Diagnostics.Stopwatch]::Frequency)
            $completion.last_audio_ago_ms = [long][math]::Round(($completedAt - $observation.last) * 1000.0 / [System.Diagnostics.Stopwatch]::Frequency)
        }
        Write-Protocol $completion
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
    if ($OutputDirectory -and (-not [System.IO.Path]::IsPathRooted($OutputDirectory) -or
        -not [System.IO.Directory]::Exists($OutputDirectory))) { throw 'OutputDirectory must be an existing absolute scratch directory.' }
    $script:voice = New-Object -ComObject SAPI.SpVoice
    $script:defaultToken = $script:voice.Voice
    if ($null -eq $script:defaultToken) { throw 'The OS has no default SAPI5 voice.' }
    Update-Catalogue
    $initialVoices = @(
        foreach ($entry in $script:catalogue) {
            @{ id = $entry.name; name = $entry.name; identity = $entry.identity
               isDefault = ($entry.identity -ceq [string]$script:defaultToken.Id) }
        }
    )
    Write-Protocol @{ event = 'ready'; voices = $initialVoices }
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
        Stop-AudioObservation $turn
        try { Write-Protocol @{ id = $turn.id; event = 'error'; error = $message } } catch { }
    }
    while ($script:queue.Count -gt 0) {
        $turn = $script:queue.Dequeue()
        try { Write-Protocol @{ id = $turn.id; event = 'error'; error = $message } } catch { }
    }
}
finally {
    if ($null -ne $script:active) { Stop-AudioObservation $script:active }
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
