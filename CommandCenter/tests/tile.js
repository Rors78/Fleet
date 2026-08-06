// Reproduce the tile logic exactly as it now ships.
function tile(ch, isErr){
  var fails  = isErr ? "--" : (ch.failed_today || 0);
  var unconf = isErr ? "--" : (ch.unconfigured_today || 0);
  var text = (isErr || !unconf) ? fails : (fails + " +" + unconf + " no-ch");
  var c    = (fails > 0) ? "#ef4444" : (unconf > 0) ? "#f59e0b" : "#22c55e";
  var lbl  = (fails > 0) ? "Failed" : (unconf > 0) ? "No Channel" : "Failed";
  return {text:String(text), color:c, label:lbl};
}
var fails=0;
function check(n,c,d){ console.log((c?'  PASS  ':'  FAIL  ')+n+(d?'  — '+d:'')); if(!c)fails++; }

console.log('=== CASE 1: the live state on the screenshot ===');
var a=tile({free_sent_today:5,failed_today:0,unconfigured_today:5},false);
console.log('     "'+a.text+'"  '+a.color+'  label="'+a.label+'"');
check('no red alarm', a.color!=='#ef4444', 'was hardcoded #ef4444 in the markup');
check('amber for a config gap', a.color==='#f59e0b');
check('label says No Channel, not Failed', a.label==='No Channel',
      'a tile reading 0 failures said FAILED');

console.log('');
console.log('=== CASE 2: a REAL delivery failure must still alarm ===');
var b=tile({free_sent_today:3,failed_today:2,unconfigured_today:5},false);
console.log('     "'+b.text+'"  '+b.color+'  label="'+b.label+'"');
check('red on real failures', b.color==='#ef4444', 'the alarm must survive the fix');
check('label reverts to Failed', b.label==='Failed');
check('both counts visible', b.text==='2 +5 no-ch', b.text);

console.log('');
console.log('=== CASE 3: everything healthy and fully configured ===');
var c=tile({free_sent_today:9,paid_sent_today:9,failed_today:0,unconfigured_today:0},false);
console.log('     "'+c.text+'"  '+c.color+'  label="'+c.label+'"');
check('green when clean', c.color==='#22c55e');
check('shows a bare 0', c.text==='0');

console.log('');
console.log('=== CASE 4: broadcaster unreachable ===');
var d=tile({},true);
check('renders -- not 0', d.text==='--', 'unknown is not zero');

console.log('');
console.log(fails?(fails+' FAILED'):'ALL PASS — the alarm still fires; only the false one is gone');
process.exit(fails?1:0);
