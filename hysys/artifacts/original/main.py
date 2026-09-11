from hysys_control import HYSYSControl

hysys = HYSYSControl("mjh_ATM.hsc")

hysys.get_operating_point("data_read")

hysys.set_mv("data_write")

state = hysys.get_convergence_status()

if state == 0:
    hysys.resume()

if state == 1:
    hysys.suspend()
    print("收敛失败")

if state == 2:
    print("收敛成功")

