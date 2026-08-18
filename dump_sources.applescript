set listName to "Daily Digest"
set outFile to (POSIX path of (path to home folder)) & "sources.tsv"

set out to "title" & tab & "notes" & linefeed

tell application "Reminders"
	tell list listName
		repeat with r in (every reminder whose completed is false)
			set t to name of r
			set b to body of r
			if b is missing value then set b to ""
			-- flatten newlines in notes so each reminder stays one row
			set b to my flatten(b)
			set out to out & t & tab & b & linefeed
		end repeat
	end tell
end tell

do shell script "cat > " & quoted form of outFile & " <<'EOF'
" & out & "
EOF"

return out

on flatten(txt)
	set AppleScript's text item delimiters to {return, linefeed, tab}
	set parts to text items of txt
	set AppleScript's text item delimiters to " "
	set result to parts as text
	set AppleScript's text item delimiters to ""
	return result
end flatten