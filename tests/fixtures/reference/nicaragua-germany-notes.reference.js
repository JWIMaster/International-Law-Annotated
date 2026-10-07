// v6 notes header: attach to window, avoid const-scoped globals
window.NOTES = window.NOTES || {};
function addNote(para, author, text, title="", source="") {
  if (!window.NOTES[para]) window.NOTES[para] = [];
  window.NOTES[para].push({ author, title, text, source });
}
addNote("para-18", "Jane Doe", "Test", "", "");
addNote("para-2", "Oscar Pearce", "Note that Nicaragua has not strictly limited its claims to violations arising out of the post-October 7 2023 conflict. Rather, as is even more explicit in their application instituting proceedings, they consider Germany's pre-2023 support for Israel to also be unlawful. This position has been lended greater weight by the Advisory Opinion issued by the Court months later finding that Israel had perpetrated - and was continuing to perpetrate - various IHRL and IHL violations in the Occupied Palestinian Territory.", "", "");
addNote("para-15", "Oscar Pearce", "It is notable that Germany here does not take the stronger position that Common Article 1 of the Geneva Conventions creates NO obligations with respect to other states. Those that adopt that more radical stance contend that the duty to ensure respect for IHL extends only to those under a state's direct influence (ie, individuals, corporations, etc).", "", "");
addNote("para-18", "Oscar Pearce", "This decrease in exports from Germany reversed in the aftermath of this decision, which could undermine Germany's case at the merits stage of these proceedings. See Judge Tladi's declaration, and this article: https://www.justsecurity.org/114479/german-arms-exports-israel-icj/.", "", "");
addNote("para-19", "Oscar Pearce", "Germany resumed funding for UNRWA shortly after these proceedings, which suggests this is unlikely to be an issue moving forward.", "", "");
addNote("para-24", "Oscar Pearce", "Of greatest global relevance here is, of course, the Arms Trade Treaty, in particular Articles 6 (which prohibits certain transfers) and 7 (which imposes certain due diligence requirements.", "", "");
addNote("para-25", "Oscar Pearce", "As to admissibility, the so-called 'Monetary Gold Principle' is likely to be at issue. Israel is not a party to these proceedings despite arguably having legal interests at stake (ie, in a hypothetical finding that it is committing certain violations).", "", "");



console.log('✅ notes loaded', Object.keys(window.NOTES));

;(function(){ window.NOTES_READY = true; window.dispatchEvent(new Event('notes:ready')); })();
